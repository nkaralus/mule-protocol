"""bridge/: DLQ, run-mode state, node swaps (sequential) and start_vram.ps1 -DryRun planning."""
import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"
sys.path.insert(0, str(BRIDGE))

import bus  # noqa: E402
import gemma_sensor as gs  # noqa: E402
import mule_tool  # noqa: E402
from test_bridge_bus import FID, eyes_block, fake_server, go, png, py, reply_for, says, seq  # noqa: E402,F401

POWERSHELL = shutil.which("powershell")
needs_ps = pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")


def dlq_lines(tmp_path):
    p = tmp_path / "dlq.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


@pytest.fixture
def health_server():
    """GET /health -> 200 on a random port. Returns the base URL."""
    servers = []

    def make():
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/health" else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield make
    for s in servers:
        s.shutdown()
        s.server_close()


# ---- DLQ --------------------------------------------------------------------
def test_three_strike_failure_is_logged_to_the_dlq(py, tmp_path):
    rec = go(py, 3, 3, seq("garbage"))
    assert not rec["ok"] and rec["dlq"] == (tmp_path / "dlq.jsonl").as_posix()
    (entry,) = dlq_lines(tmp_path)
    assert entry["kind"] == "worker_naks" and entry["frame_id"] == FID and entry["lines"] == "3:3"
    assert entry["attempts"] == 3 and len(entry["naks"]) == 3 and entry["naks"][0]["gate"] == "GI_1"
    assert entry["instruction"] == "do it" and entry["file"].endswith("m.py") and entry["ts"].endswith("+00:00")
    assert "x = 1" not in json.dumps(entry)          # file contents are never copied into the ledger


def test_exhausting_a_lowered_retry_budget_is_also_a_strike(py, tmp_path):
    go(py, 3, 3, seq("garbage"), retries=1)
    assert dlq_lines(tmp_path)[0]["attempts"] == 1


def test_dlq_appends_and_only_records_the_media_basename(py, png, tmp_path):
    go(py, 3, 3, seq("garbage"))
    go(py, 3, 3, seq("garbage"))
    assert len(dlq_lines(tmp_path)) == 2

    def bad_eyes(*a, **k):
        raise gs.SensorError("2 attempts failed GI_1: confidence")
    rec = bus.run_task(str(py), 3, 3, "look", media=str(png), roots=[py.parent], look=bad_eyes,
                       generate=seq("never"), frame_id=FID)
    last = dlq_lines(tmp_path)[-1]
    assert not rec["ok"] and last["kind"] == "eyes_invalid" and last["media"] == "shot.png"
    assert str(png.parent) not in json.dumps(last)


def test_nothing_is_logged_for_success_scope_refusal_or_dead_server(py, tmp_path):
    assert go(py, 3, 3, says("x = 2\n"))["ok"]
    assert not bus.run_task(str(py), 3, 3, "x", roots=[tmp_path / "elsewhere"], generate=seq("n"))["ok"]

    def boom(*a, **k):
        raise ConnectionError("refused")
    with pytest.raises(bus.NodeDown):
        go(py, 3, 3, boom)
    assert dlq_lines(tmp_path) == []


def test_unwritable_dlq_does_not_break_the_receipt(py, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("MULE_DLQ", str(blocker / "dlq.jsonl"))      # a file where a directory is needed
    rec = go(py, 3, 3, seq("garbage"))
    assert not rec["ok"] and rec["dlq"] is None and "3 NAKs" in rec["error"]


def test_dlq_cli_and_reject_message(py, fake_server, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MULE_ALLOWED_ROOTS", str(py.parent))
    bad = fake_server("garbage")
    assert mule_tool.main([str(py), "3", "3", "x", "--url", bad["url"]]) == 1
    assert "Logged to the DLQ" in capsys.readouterr().out
    assert mule_tool.main(["dlq", "--tail", "5"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("DLQ: 1 shown") and json.loads(out[1])["kind"] == "worker_naks"


# ---- mode file, urls, status ------------------------------------------------
def write_mode(tmp_path, **kw):
    (tmp_path / "mode.json").write_text(json.dumps(kw), encoding="utf-8")


def test_urls_prefer_env_then_mode_file_then_defaults(tmp_path, monkeypatch):
    assert bus.load_mode() == {} and bus.default_urls() == (bus.DEFAULT_WORKER_URL, bus.DEFAULT_EYES_URL)
    write_mode(tmp_path, mode="overnight-26b", worker_url="http://127.0.0.1:9001", eyes_url="http://127.0.0.1:9001")
    assert bus.default_urls() == ("http://127.0.0.1:9001", "http://127.0.0.1:9001")
    monkeypatch.setenv("MULE_EYES_URL", "http://127.0.0.1:9002")
    assert bus.default_urls() == ("http://127.0.0.1:9001", "http://127.0.0.1:9002")


def test_mode_file_with_bom_or_garbage_is_tolerated(tmp_path):
    (tmp_path / "mode.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"mode": "sequential"}).encode())
    assert bus.load_mode()["mode"] == "sequential"      # PowerShell 5.1 writes a BOM
    (tmp_path / "mode.json").write_text("{not json", encoding="utf-8")
    assert bus.load_mode() == {}


def test_status_reports_up_down_and_on_demand(tmp_path, health_server, capsys):
    up = health_server()
    write_mode(tmp_path, mode="worker-only", worker_url=up, eyes_url="http://127.0.0.1:9")
    s = bus.node_status()
    assert (s["mode"], s["worker"], s["eyes"]) == ("worker-only", "up", "down")
    write_mode(tmp_path, mode="sequential", worker_url=up, eyes_url="http://127.0.0.1:9")
    assert bus.node_status()["eyes"] == "on-demand"
    assert mule_tool.main(["status"]) == 0
    line = capsys.readouterr().out
    assert line.startswith("STATUS: ") and json.loads(line[len("STATUS: "):])["eyes"] == "on-demand"


# ---- swap hook ordering -----------------------------------------------------
def test_swap_boots_eyes_before_the_look_and_worker_before_execute(py, png):
    events = []

    def look(media, question, **kw):
        events.append("look")
        return {"ref": "10.2.1.1", "observations": ["a"], "ocr_text": "", "confidence": 50}

    def gen(messages, grammar, **kw):
        events.append("generate")
        return reply_for(messages, "x = 2\n")

    rec = bus.run_task(str(py), 3, 3, "x", media=str(png), roots=[py.parent], look=look, generate=gen,
                       swap=lambda phase: events.append(f"swap:{phase}"), frame_id=FID)
    assert rec["ok"] and events == ["swap:eyes", "look", "swap:worker", "generate"]
    assert rec["swaps"] == ["eyes", "worker"]


def test_without_media_only_the_worker_is_swapped_in(py):
    events = []
    rec = go(py, 3, 3, says("x = 2\n"), swap=events.append)
    assert rec["ok"] and events == ["worker"]


def test_swapper_exists_only_in_sequential_mode(tmp_path):
    assert bus.swapper() is None
    write_mode(tmp_path, mode="worker-only", script="x")
    assert bus.swapper() is None
    write_mode(tmp_path, mode="sequential", script="x")
    assert bus.swapper() is not None


@needs_ps
def test_swap_runs_the_script_and_reports_a_failing_one_as_nodedown(tmp_path, monkeypatch):
    ok = tmp_path / "ok.ps1"
    ok.write_text('param([string]$Mode,[string]$Phase)\nAdd-Content $env:SWAP_LOG "$Mode/$Phase"\nexit 0\n')
    bad = tmp_path / "bad.ps1"
    bad.write_text('param([string]$Mode,[string]$Phase)\nWrite-Output "VRAM would overflow"\nexit 3\n')
    monkeypatch.setenv("SWAP_LOG", str(tmp_path / "swaps.log"))
    write_mode(tmp_path, mode="sequential", script=str(ok))
    bus.swapper()("eyes")
    bus.swapper()("worker")
    assert (tmp_path / "swaps.log").read_text().split() == ["sequential/eyes", "sequential/worker"]
    write_mode(tmp_path, mode="sequential", script=str(bad))
    with pytest.raises(bus.NodeDown, match="VRAM would overflow") as e:
        bus.swapper()("worker")
    assert e.value.node == "worker"


@needs_ps
def test_swap_never_reads_the_script_through_pipes(tmp_path, monkeypatch):
    """Regression. With capture_output=True the swap hung forever: the llama-server that start_vram.ps1
    launches outlives the script and keeps the pipe open, so the read never sees EOF (and on Windows even
    subprocess.run's own timeout hangs on its second read). A synthetic child does not reproduce it, only a
    real llama-server launch does, so this pins the property instead: no pipes, stdin closed, a timeout."""
    script = tmp_path / "s.ps1"
    script.write_text("exit 0\n")
    write_mode(tmp_path, mode="sequential", script=str(script))
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(bus.subprocess, "run", fake_run)
    bus.swapper()("worker")
    assert not seen.get("capture_output") and seen["stdout"] not in (None, subprocess.PIPE)
    assert seen["stderr"] == subprocess.STDOUT and seen["stdin"] == subprocess.DEVNULL
    assert 0 < seen["timeout"] <= 900


@needs_ps
def test_sequential_cli_round_trip_swaps_in_order(py, png, fake_server, tmp_path, monkeypatch, capsys):
    script = tmp_path / "swap.ps1"
    script.write_text('param([string]$Mode,[string]$Phase)\nAdd-Content $env:SWAP_LOG $Phase\nexit 0\n')
    monkeypatch.setenv("SWAP_LOG", str(tmp_path / "swaps.log"))
    monkeypatch.setenv("MULE_ALLOWED_ROOTS", str(py.parent))
    eyes = fake_server(lambda body: eyes_block(frame_id=json.loads(
        body["messages"][0]["content"][0]["text"].split("STATE: ", 1)[1].split("\nOUTPUT:", 1)[0])["frame_id"]))
    worker = fake_server(lambda body: reply_for(body["messages"], "x = 5\n"))
    write_mode(tmp_path, mode="sequential", worker_url=worker["url"], eyes_url=eyes["url"], script=str(script))
    assert mule_tool.main([str(py), "3", "3", "use the shot", "--media", str(png)]) == 0
    receipt = json.loads(capsys.readouterr().out.split("RECEIPT: ", 1)[1])
    assert receipt["swaps"] == ["eyes", "worker"] and receipt["eyes"]["confidence"] == 88
    assert (tmp_path / "swaps.log").read_text().split() == ["eyes", "worker"]
    assert mule_tool.main(["inspect", str(png), "q"]) == 0          # inspect also boots the eyes node first
    assert (tmp_path / "swaps.log").read_text().split() == ["eyes", "worker", "eyes"]


# ---- start_vram.ps1 -DryRun planning ----------------------------------------
@pytest.fixture
def ps_env(tmp_path):
    """A fake runtime + model files so the script's preflight and flag building run without a GPU."""
    rt = tmp_path / "rt"
    rt.mkdir()
    (rt / "llama-server.exe").write_bytes(b"")
    files = {}
    for name, mib in (("qwen", 50), ("gemma", 30), ("mmproj", 5), ("big", 100), ("bigmm", 5)):
        p = tmp_path / f"{name}.gguf"
        with open(p, "wb") as f:
            f.truncate(mib * 1024 * 1024)
        files[name] = str(p)
    mode_file = tmp_path / "ps_mode.json"

    def run(*args):
        cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BRIDGE / "start_vram.ps1"),
               "-RuntimeDir", str(rt), "-Model", files["qwen"], "-GemmaModel", files["gemma"],
               "-GemmaMmproj", files["mmproj"], "-Big26bModel", files["big"], "-Big26bMmproj", files["bigmm"],
               "-DryRun", *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           env={**__import__("os").environ, "MULE_MODE_FILE": str(mode_file)})
        return r.returncode, r.stdout + r.stderr
    run.mode_file = mode_file
    run.files = files
    return run


@needs_ps
def test_dryrun_worker_only_plans_one_launch(ps_env):
    rc, out = ps_env("-Mode", "worker-only")
    assert rc == 0 and out.count("DRYRUN launch") == 1 and "--port 8080" in out and "--mmproj" not in out
    assert not ps_env.mode_file.exists()                       # a dry run saves nothing


@needs_ps
def test_dryrun_sequential_phases_boot_exactly_one_node_each(ps_env):
    rc, eyes = ps_env("-Mode", "sequential", "-Phase", "eyes")
    assert rc == 0 and eyes.count("DRYRUN launch") == 1
    assert "launch eyes" in eyes and "--port 8081" in eyes and "--mmproj" in eyes and "-ngl 99" in eyes
    rc, worker = ps_env("-Mode", "sequential", "-Phase", "worker")
    assert rc == 0 and worker.count("DRYRUN launch") == 1
    assert "launch worker" in worker and "--port 8080" in worker and "--mmproj" not in worker


@needs_ps
def test_dryrun_concurrent_plans_both_nodes(ps_env):
    rc, out = ps_env("-Mode", "concurrent")
    assert rc == 0 and out.count("DRYRUN launch") == 2 and "--port 8080" in out and "--port 8081" in out


@needs_ps
def test_dryrun_overnight_26b_pins_hot_experts_and_serves_both_roles(ps_env):
    rc, out = ps_env("-Mode", "overnight-26b")
    assert rc == 0 and out.count("DRYRUN launch") == 1 and "launch big26b" in out
    assert "--n-cpu-moe 22" in out and "--mmproj" in out and "--port 8080" in out and "-ngl 99" in out
    rc, out = ps_env("-Mode", "overnight-26b", "-HotExpertLayers", "30")
    assert "--n-cpu-moe 0" in out
    rc, out = ps_env("-Mode", "overnight-26b", "-OverrideTensor", "blk.2.ffn_.*_exps.=CPU")
    assert "-ot blk.2.ffn_.*_exps.=CPU" in out and "--n-cpu-moe" not in out


@needs_ps
def test_overnight_26b_without_a_gguf_explains_the_conversion(tmp_path):
    rt = tmp_path / "rt"
    rt.mkdir()
    (rt / "llama-server.exe").write_bytes(b"")
    q = tmp_path / "q.gguf"
    q.write_bytes(b"x")
    r = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(BRIDGE / "start_vram.ps1"), "-Mode", "overnight-26b", "-RuntimeDir", str(rt),
                        "-Model", str(q), "-Big26bModel", str(tmp_path / "missing.gguf"), "-DryRun"],
                       capture_output=True, text=True, timeout=120)
    text = r.stdout + r.stderr
    assert r.returncode != 0 and "safetensors" in text and "convert_hf_to_gguf.py" in text
    assert "llama-quantize" in text and "DRYRUN launch" not in text


@needs_ps
def test_missing_sensor_files_fail_before_anything_is_touched(tmp_path):
    rt = tmp_path / "rt"
    rt.mkdir()
    (rt / "llama-server.exe").write_bytes(b"")
    q = tmp_path / "q.gguf"
    q.write_bytes(b"x")
    r = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(BRIDGE / "start_vram.ps1"), "-Mode", "sequential", "-Phase", "eyes", "-RuntimeDir",
                        str(rt), "-Model", str(q), "-GemmaModel", str(tmp_path / "nope.gguf"), "-DryRun"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0 and "file not found" in (r.stdout + r.stderr) and "Stopping" not in r.stdout


@needs_ps
@pytest.mark.skipif(shutil.which("nvidia-smi") is None, reason="needs an NVIDIA GPU for the budget figure")
def test_budget_refusal_happens_before_anything_would_be_stopped(ps_env):
    rc, out = ps_env("-Mode", "concurrent", "-PerServerOverheadMiB", "999999")
    assert rc != 0 and "does not fit" in out and "Nothing was stopped" in out and "DRYRUN launch" not in out
    rc, out = ps_env("-Mode", "concurrent", "-PerServerOverheadMiB", "999999", "-Force")
    assert rc == 0 and out.count("DRYRUN launch") == 2
