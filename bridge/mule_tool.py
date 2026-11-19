#!/usr/bin/env python3
"""CLI entrypoint for the Claude Code <-> local worker bus.

    mule_tool.py <file> <start_line> <end_line> "<spec>" [--media <path>]   patch a line slice
    mule_tool.py inspect <media> "<question>"                               look at media only
    mule_tool.py status                                                     which nodes can serve now
    mule_tool.py dlq [--tail N]                                             failed tasks (dlq.jsonl)
    mule_tool.py complete <frame.json> [--root DIR]                         completion gates GI_4/GI_5

A patch runs through bridge/bus.py (SLOT_1_TASK .. SLOT_5_VERIFY, gates GI_1..GI_3, up to 3 NAK
retries on the worker) and prints a unified diff, an `OK:` / `REJECTED BY MULE:` line and a one-line
`RECEIPT:` JSON. With --media the eyes node (Gemma, :8081) reports on the screenshot or audio first
and the worker receives that report as untrusted context. Review the diff: a valid patch can still
be the wrong patch.

Exit codes: 0 done, 1 rejected (file untouched), 2 a model server is not answering.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path
from typing import List, Optional

_HERE = Path(__file__).resolve().parent
for _d in (_HERE / "mule_bridge", _HERE):   # installed layout first, then the repo's bridge/ folder
    if (_d / "bus.py").is_file():
        sys.path.insert(0, str(_d))
        break

import bus  # noqa: E402
import gemma_sensor  # noqa: E402


def _common(ap: argparse.ArgumentParser) -> None:
    worker_url, eyes_url = bus.default_urls()   # env, then the mode file start_vram.ps1 wrote, then defaults
    ap.add_argument("--url", default=worker_url, help="worker (Qwen, or the 26B in overnight mode)")
    ap.add_argument("--eyes-url", default=eyes_url, help="sensor (Gemma)")
    ap.add_argument("--timeout", type=float, default=300.0)


def _server_error(e: bus.NodeDown) -> int:
    print(f"MULE SERVER ERROR: {e}. Start it with bridge/start_vram.ps1 "
          f"-Mode worker-only | sequential | concurrent | overnight-26b")
    return 2


def cmd_inspect(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="mule_tool.py inspect")
    ap.add_argument("media")
    ap.add_argument("question")
    _common(ap)
    a = ap.parse_args(argv)
    try:
        swap = bus.swapper()
        if swap:
            swap("eyes")   # sequential mode: only one model fits in VRAM, boot the sensor now
        report = gemma_sensor.inspect(a.media, a.question, url=a.eyes_url, timeout=a.timeout)
    except bus.NodeDown as e:
        return _server_error(e)
    except gemma_sensor.SensorError as e:
        print(f"REJECTED BY MULE: eyes node: {e}")
        return 1
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        return _server_error(bus.NodeDown("eyes", a.eyes_url, e))
    print(f"OK: eyes report from {report['ref']}, confidence {report['confidence']}")
    print("EYES: " + json.dumps({k: report[k] for k in ("ref", "observations", "ocr_text", "confidence")},
                                 separators=(",", ":")))
    return 0


def cmd_patch(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="mule_tool.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("start_line", type=int)
    ap.add_argument("end_line", type=int)
    ap.add_argument("spec", help="exact edit instruction")
    ap.add_argument("--media", help="screenshot (png/jpg/webp/gif) or audio (wav/mp3) for the eyes node")
    ap.add_argument("--model", default=bus.DEFAULT_MODEL)
    ap.add_argument("--retries", type=int, default=bus.MAX_RETRIES, help="worker NAK retries, max 3")
    ap.add_argument("--allow-root", action="append", default=[], metavar="DIR",
                    help="extra directory the worker may edit in (default: your home folder, "
                         "or MULE_ALLOWED_ROOTS)")
    ap.add_argument("--dry-run", action="store_true", help="run every gate, write nothing")
    _common(ap)
    a = ap.parse_args(argv)
    roots = bus.default_roots() + [Path(r) for r in a.allow_root]
    try:
        rec = bus.run_task(a.file, a.start_line, a.end_line, a.spec, media=a.media, roots=roots,
                           url=a.url, eyes_url=a.eyes_url, model=a.model, retries=a.retries,
                           timeout=a.timeout, dry_run=a.dry_run, swap=bus.swapper(),
                           log=lambda s: print(s, file=sys.stderr))
    except bus.NodeDown as e:
        return _server_error(e)
    if rec["ok"]:
        if rec.get("diff"):
            print(rec["diff"].rstrip("\n"))
        verb = "patched" if rec["wrote"] else "would patch"
        print(f"OK: {verb} {rec['file']} [{rec['lines']}] attempts: {rec['attempts']}, "
              f"naks: {len(rec['naks'])}, syntax: {rec['gates'].get('GI_3')}")
    else:
        print(f"REJECTED BY MULE: {rec['error']}. File untouched."
              + (f" Logged to the DLQ: {rec['dlq']}" if rec.get("dlq") else ""))
    print("RECEIPT: " + bus.receipt_json(rec))
    return 0 if rec["ok"] else 1


def cmd_status(argv: List[str]) -> int:
    """One JSON line. eyes/worker are up | down | on-demand (sequential mode boots them when needed)."""
    argparse.ArgumentParser(prog="mule_tool.py status").parse_args(argv)
    print("STATUS: " + json.dumps(bus.node_status(), separators=(",", ":")))
    return 0


def cmd_dlq(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="mule_tool.py dlq")
    ap.add_argument("--tail", type=int, default=20)
    a = ap.parse_args(argv)
    entries = bus.read_dlq(a.tail)
    print(f"DLQ: {len(entries)} shown from {bus.dlq_path().as_posix()}")
    for e in entries:
        print(json.dumps(e, separators=(",", ":")))
    return 0


def cmd_complete(argv: List[str]) -> int:
    """Check a completion frame against the files: declared artifacts real, runtime wired. No model involved."""
    import completion
    ap = argparse.ArgumentParser(prog="mule_tool.py complete")
    ap.add_argument("frame", help="completion frame: a JSON file (one strict mule block)")
    ap.add_argument("--root", default=".", help="directory the frame's paths are relative to (default: cwd)")
    a = ap.parse_args(argv)
    try:
        raw = Path(a.frame).read_text(encoding="utf-8").strip()
    except OSError as e:
        print(f"REJECTED BY MULE: cannot read {a.frame}: {e}")
        return 1
    rec = completion.verify_completion(raw, Path(a.root))
    if rec["ok"]:
        print(f"OK: completion verified for frame {rec['frame_id']} under {rec['root']}")
    else:
        print("REJECTED BY MULE: task is not complete.")
        for e in rec["errors"]:
            print(f"  {e}")
    print("RECEIPT: " + completion.receipt_json(rec))
    return 0 if rec["ok"] else 1


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmds = {"inspect": cmd_inspect, "status": cmd_status, "dlq": cmd_dlq, "complete": cmd_complete}
    if argv and argv[0] in cmds:
        return cmds[argv[0]](argv[1:])
    return cmd_patch(argv)


if __name__ == "__main__":
    sys.exit(main())
