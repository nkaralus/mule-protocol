"""bridge/completion.py: GI_4 (artifact / extraction completeness) and GI_5 (dead wiring, stub-only tests)."""
import json
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"
sys.path.insert(0, str(BRIDGE))

import bus  # noqa: E402
import completion as cg  # noqa: E402
import mule_tool  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
FID = "0x0c0d"


def png_bytes(w, h):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def put(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    (p.write_bytes if isinstance(data, bytes) else lambda d: p.write_text(d, encoding="utf-8"))(data)
    return p


@pytest.fixture
def repo(tmp_path):
    """A git repo with a reference asset and a runtime entry point committed at `base`."""
    root = tmp_path / "pack"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "user.name", "t")
    git(root, "config", "core.autocrlf", "false")
    put(root, "ref/car.png", png_bytes(16, 16))
    put(root, "scripts/main.js", "import { world } from '@minecraft/server';\nexport const REGISTRATIONS = [];\n")
    put(root, "tests/test_main.py", "def test_x():\n    assert True\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return root, base


def finish(root):
    """What a complete task leaves behind: the asset copied, a real script, the entry point wired."""
    shutil.copyfile(root / "ref/car.png", put(root, "RP/textures/car.png", b"x"))
    put(root, "scripts/car.js", "export function registerCar(world) {\n  return world.getDimension('overworld');\n}\n")
    put(root, "scripts/main.js", "import { world } from '@minecraft/server';\nimport { registerCar } from './car.js';\n"
                                 "export const REGISTRATIONS = [registerCar];\n")


def frame(base, **kw):
    kw.setdefault("artifacts", ["scripts/car.js"])
    kw.setdefault("copies", ["ref/car.png>RP/textures/car.png"])
    kw.setdefault("runtime", ["scripts/main.js"])
    kw.setdefault("hooks", ["scripts/main.js::REGISTRATIONS = [registerCar]"])
    return bus.compact(cg.completion_frame(FID, base=base, **kw))


def errors(repo_and_base, **kw):
    root, base = repo_and_base
    rec = cg.verify_completion(frame(base, **kw), root)
    return rec, " | ".join(rec["errors"])


# ---- happy path and GI_1 ------------------------------------------------------
def test_complete_task_passes_all_gates(repo):
    finish(repo[0])
    rec, errs = errors(repo)
    assert rec["ok"], errs
    assert rec["gates"] == {"GI_1": "echo ok", "GI_4": "ok", "GI_5": "ok"}


def test_committed_work_after_base_still_counts(repo):
    finish(repo[0])
    git(repo[0], "add", "-A")
    git(repo[0], "commit", "-q", "-m", "work")
    assert errors(repo)[0]["ok"]


def test_completion_frame_obeys_the_protocol(repo):
    raw = frame(repo[1])
    assert cg.gi1_frame(raw)["op"] == "verify"


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(extra=1),
    lambda b: b.update(op="patch_slice"),
    lambda b: b.update(ref="10.3.1.1"),
    lambda b: b.update(base="HEAD"),
    lambda b: b.update(artifacts=["has space.js"]),
    lambda b: b.update(hooks=["scripts/main.js"]),            # no ::needle
    lambda b: b.update(tests_passed="yes"),
    lambda b: b.pop("skipped"),
    lambda b: b.update(frame_id="nope"),
])
def test_gi1_rejects_malformed_completion_frames(repo, mutate):
    block = cg.completion_frame(FID, base=repo[1])
    mutate(block)
    rec = cg.verify_completion(bus.compact(block), repo[0])
    assert not rec["ok"] and rec["gates"] == {"GI_1": "fail"}


def test_gi1_rejects_backslashes_and_garbage(repo):
    for raw in (frame(repo[1]).replace("scripts/car.js", "scripts\\\\car.js"), "not json", "[]"):
        assert cg.verify_completion(raw, repo[0])["gates"] == {"GI_1": "fail"}


# ---- GI_4: artifact / reference extraction completeness -------------------------
def test_gi4_skipped_or_deferred_work_is_rejected(repo):
    finish(repo[0])
    rec, errs = errors(repo, skipped=["car.geo.json deferred to user"])
    assert not rec["ok"] and rec["gates"]["GI_4"] == "fail" and "skipped / deferred" in errs


def test_gi4_missing_artifact(repo):
    finish(repo[0])
    rec, errs = errors(repo, artifacts=["scripts/car.js", "RP/models/car.geo.json"])
    assert "declared artifact RP/models/car.geo.json was not produced" in errs


def test_gi4_uncopied_source_asset(repo):
    finish(repo[0])
    (repo[0] / "RP/textures/car.png").unlink()
    assert "was not copied to RP/textures/car.png (uncopied)" in errors(repo)[1]


def test_gi4_copy_replaced_by_a_placeholder(repo):
    finish(repo[0])
    put(repo[0], "RP/textures/car.png", png_bytes(1, 1))
    rec, errs = errors(repo)
    assert "is not a copy of ref/car.png" in errs and rec["gates"]["GI_4"] == "fail"


def test_gi4_missing_source_asset(repo):
    finish(repo[0])
    assert "source asset ref/wheel.png does not exist" in errors(repo, copies=["ref/wheel.png>RP/w.png"])[1]


@pytest.mark.parametrize("rel,data,why", [
    ("scripts/car.js", "", "is empty"),
    ("scripts/car.js", "export function registerCar() {\n  // TODO: wire the car\n  return 1;\n}\n", "stub marker 'TODO'"),
    ("scripts/car.js", "export function registerCar(world) {}\n", "has an empty function"),
    ("scripts/car.js", "// placeholder until the real model lands\nexport const x = 1;\n", "stub marker 'placeholder'"),
    ("scripts/car.py", 'def register(world):\n    """Wire it."""\n    pass\n', "stub function(s) register"),
    ("scripts/car.py", "def register(world):\n    raise NotImplementedError\n", "NotImplementedError"),
    ("scripts/car.py", "def register(world):\n    ...\n", "stub function(s) register"),
    ("RP/car.json", "{}", "empty JSON placeholder"),
    ("RP/car.png", png_bytes(2, 2), "2x2 placeholder image"),
    ("RP/car.png", b"GIF89a" + b"\x00" * 30, "not a valid PNG"),
    ("RP/car.jpg", b"\x89PNG", "not a valid JPEG"),
])
def test_gi4_stub_and_placeholder_artifacts(repo, rel, data, why):
    finish(repo[0])
    put(repo[0], rel, data)
    rec, errs = errors(repo, artifacts=[rel])
    assert not rec["ok"] and why in errs, errs


def test_gi4_real_files_are_not_stubs(repo):
    root = repo[0]
    for rel, data in (("a.py", "def f(x):\n    return x + 1\n"), ("b.json", '{"k": 1}'), ("c.png", png_bytes(16, 16)),
                      ("d.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 20), ("e.bin", b"\x00\x01"),
                      ("f.js", "export const noop = () => {};\nexport function g(a) { return a; }\n")):
        assert cg.stub_reason(put(root, rel, data)) is None, rel


def test_gi4_path_climbing_is_refused(repo):
    finish(repo[0])
    assert "climbs the tree" in errors(repo, artifacts=["scripts/../../etc/passwd"])[1]


# ---- GI_5: stub-only tests and dead wiring ------------------------------------
def test_gi5_runtime_file_not_modified(repo):
    root = repo[0]
    finish(root)
    git(root, "checkout", "--", "scripts/main.js")       # the entry point is back to base
    rec, errs = errors(repo, hooks=[])
    assert rec["gates"]["GI_5"] == "fail" and "runtime file scripts/main.js was not modified since" in errs


def test_gi5_stub_only_tests_even_when_tests_pass(repo):
    root = repo[0]
    put(root, "tests/test_car.py", "def test_car():\n    assert 1 + 1 == 2\n")
    rec, errs = errors(repo, artifacts=[], copies=[], hooks=[], tests_passed=True)
    assert not rec["ok"] and "only test files changed" in errs
    assert "tests_passed=true does not override this" in errs


def test_gi5_missing_hook_is_dead_wiring(repo):
    root = repo[0]
    finish(root)
    put(root, "scripts/main.js", "import { world } from '@minecraft/server';\nexport const REGISTRATIONS = [];\n// x\n")
    rec, errs = errors(repo)
    assert "required runtime hook 'REGISTRATIONS = [registerCar]' is missing from scripts/main.js (dead wiring)" in errs


def test_gi5_hook_only_in_a_comment_does_not_count(repo):
    root = repo[0]
    finish(root)
    put(root, "scripts/main.js", "export const REGISTRATIONS = [];\n// REGISTRATIONS = [registerCar]\n"
                                 "/* REGISTRATIONS = [registerCar] */\n")
    assert "dead wiring" in errors(repo)[1]
    put(root, "scripts/hook.py", "# register_car(world)\nx = 1\n")
    assert "dead wiring" in errors(repo, hooks=["scripts/hook.py::register_car(world)"])[1]


def test_gi5_new_untracked_runtime_file_counts_as_modified(repo):
    finish(repo[0])
    rec, errs = errors(repo, runtime=["scripts/main.js", "scripts/car.js"])
    assert rec["ok"], errs


def test_gi5_runtime_file_that_is_a_stub(repo):
    root = repo[0]
    finish(root)
    put(root, "scripts/main.js", "export const REGISTRATIONS = [registerCar]; // TODO real wiring\n")
    assert "runtime file scripts/main.js contains stub marker" in errors(repo)[1]


def test_gi5_unknown_base_commit(repo):
    finish(repo[0])
    rec, errs = errors((repo[0], "deadbeef"))
    assert rec["gates"]["GI_5"] == "fail" and "base deadbeef is not a commit" in errs


def test_gi5_missing_hook_file(repo):
    finish(repo[0])
    assert "hook file scripts/nope.js does not exist" in errors(repo, hooks=["scripts/nope.js::x"])[1]


# ---- CLI ----------------------------------------------------------------------
def test_cli_complete_ok_and_rejected(repo, tmp_path, capsys):
    root, base = repo
    f = tmp_path / "frame.json"
    f.write_text(frame(base), encoding="utf-8")
    assert mule_tool.main(["complete", str(f), "--root", str(root)]) == 1      # nothing done yet
    out = capsys.readouterr().out
    assert "REJECTED BY MULE: task is not complete." in out and "uncopied" in out
    assert json.loads(out.split("RECEIPT: ", 1)[1])["gates"]["GI_4"] == "fail"
    finish(root)
    assert mule_tool.main(["complete", str(f), "--root", str(root)]) == 0
    assert "OK: completion verified for frame 0x0c0d" in capsys.readouterr().out
    assert mule_tool.main(["complete", str(tmp_path / "missing.json"), "--root", str(root)]) == 1
