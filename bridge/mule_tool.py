#!/usr/bin/env python3
"""Deterministic line-slice code patcher backed by a local llama.cpp worker.

    python mule_tool.py <file> <start_line> <end_line> "<exact edit instruction>"

Replaces lines start..end (1-based, inclusive) of <file> with code written by the
local model at http://127.0.0.1:8080. The model is forced, by a GBNF grammar from
`mule.constrain.to_gbnf()`, to emit one JSON line:

    {"target":"<file>","ref":"<start>:<end>","replacement_b64":"<base64 utf-8>"}

`target` and `ref` are pinned to the expected values inside the grammar and then
checked again by `MuleValidator.validate_raw(..., expect=...)`. Only a block that
passes both is decoded. The result is syntax-checked on a temp copy
(py_compile / node --check / json.loads) and the real file is replaced atomically,
so a bad patch can never leave a broken file behind.

Exit codes: 0 patched, 1 rejected or failed (file untouched), 2 usage / server error.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import py_compile
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    from mule import MuleValidator, runners
    from mule.constrain import to_gbnf
except ImportError:  # running from a checkout without `pip install -e`
    for _cand in (os.environ.get("MULE_PROTOCOL_HOME"), str(Path(__file__).resolve().parent.parent)):
        if _cand and (Path(_cand) / "mule" / "__init__.py").is_file():
            sys.path.insert(0, _cand)
            break
    from mule import MuleValidator, runners
    from mule.constrain import to_gbnf

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_MODEL = "qwen2.5-coder-14b"
MAX_LINES = 80          # slice size cap, per the delegation rule
MAX_B64 = 12288         # longest replacement_b64 accepted (~9 KB of code)
MAX_TOKENS = 6144       # generation budget; base64 costs well over 1 token / 3 chars
B64_PATTERN = "[A-Za-z0-9+/=]*"  # inside the regex subset that to_gbnf supports

SYSTEM = (
    "You are a deterministic code patcher, not an assistant.\n"
    "You receive a file path, a line range, an instruction and the CURRENT_SLICE of that range.\n"
    "Reply with exactly one JSON object on one line, keys in this order: target, ref, replacement_b64.\n"
    "target and ref: copy exactly from the request.\n"
    "replacement_b64: the COMPLETE new text for the range (not a diff, not the whole file), "
    "UTF-8 encoded as standard base64. Keep every unchanged line exactly as it is; "
    "CURRENT_SLICE_B64 is the old text already encoded, so reuse its base64 for unchanged lines.\n"
    "No markdown. No commentary. No line breaks."
)


class PatchError(Exception):
    """The patch was rejected. The target file has not been modified."""


# ---- spec / grammar ---------------------------------------------------------
def build_slice_spec(target: str, ref: str) -> Dict:
    """A mule spec (same shape as spec/mule_spec.json) for one slice patch."""
    return {
        "additionalProperties": False,
        "slots": {
            "target": {"type": "string", "enum": [target], "maxLength": max(len(target), 1)},
            "ref": {"type": "string", "enum": [ref], "maxLength": len(ref)},
            "replacement_b64": {"type": "string", "pattern": B64_PATTERN, "maxLength": MAX_B64},
        },
        "cross_checks": [],
        "limits": {"max_raw_bytes": MAX_B64 + len(target) + len(ref) + 128},
    }


# ---- text helpers -----------------------------------------------------------
def read_lines(path: Path) -> Tuple[List[str], str]:
    """Lines with endings preserved, plus the file's newline style."""
    text = path.read_bytes().decode("utf-8")
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if text.count("\r\n") > text.count("\n") / 2 and "\r\n" in text else "\n"
    return lines, newline


def apply_slice(lines: List[str], start: int, end: int, replacement: str, newline: str) -> str:
    """New file text with lines start..end replaced by `replacement`."""
    replacement = replacement.replace("\r\n", "\n").replace("\n", newline)
    slice_had_eol = lines[end - 1].endswith(("\n", "\r"))
    if replacement and slice_had_eol and not replacement.endswith(newline):
        replacement += newline
    return "".join(lines[:start - 1]) + replacement + "".join(lines[end:])


def syntax_check(path: Path, text: str) -> str:
    """Check `text` as it would be saved at `path`. Returns a short note; raises PatchError."""
    suffix = path.suffix.lower()
    with tempfile.TemporaryDirectory(prefix="mule_check_") as d:
        tmp = Path(d) / path.name
        tmp.write_bytes(text.encode("utf-8"))
        if suffix == ".py":
            try:
                py_compile.compile(str(tmp), cfile=str(tmp) + "c", doraise=True)
            except py_compile.PyCompileError as e:
                raise PatchError(f"py_compile failed: {str(e).strip().splitlines()[-1]}") from None
            return "py_compile"
        if suffix in (".js", ".mjs", ".cjs"):
            try:
                r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True, timeout=60)
            except FileNotFoundError:
                return "node not found, syntax check SKIPPED"
            if r.returncode != 0:
                msg = (r.stderr.strip().splitlines() or ["syntax error"])[-1]
                raise PatchError(f"node --check failed: {msg}")
            return "node --check"
        if suffix == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as e:
                raise PatchError(f"json.loads failed: {e}") from None
            return "json.loads"
    return "no syntax checker for this file type"


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".mule_tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---- model round trip -------------------------------------------------------
def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def build_messages(target: str, ref: str, instruction: str, current: str) -> List[Dict[str, str]]:
    """One worked example (encoded at runtime, so it cannot be wrong), then the real request."""
    ex_target, ex_ref, ex_old, ex_new = "demo/a.py", "7:8", "x = 1\nprint(x)\n", "y = 1\nprint(y)\n"

    def user(t, r, instr, cur):
        return (f"target={t}\nref={r}\ninstruction={instr}\n\nCURRENT_SLICE:\n{cur}\n"
                f"CURRENT_SLICE_B64 (the slice as base64; reuse it for unchanged text):\n{_b64(cur)}")

    ex_block = json.dumps({"target": ex_target, "ref": ex_ref, "replacement_b64": _b64(ex_new)},
                          separators=(",", ":"))
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user(ex_target, ex_ref, "rename x to y", ex_old)},
        {"role": "assistant", "content": ex_block},
        {"role": "user", "content": user(target, ref, instruction, current)},
    ]


def request_block(
    messages, grammar: str, *, url: str, model: str, temperature: float, timeout: float,
) -> str:
    return runners.generate(
        "llamacpp", model, messages, base_url=url, grammar=grammar, timeout=timeout,
        sampling={"temperature": temperature, "max_tokens": MAX_TOKENS},
    )


def verify_block(raw: str, spec: Dict, target: str, ref: str) -> str:
    """MuleValidator gate. Returns the decoded replacement text; raises PatchError."""
    ok, block, errs = MuleValidator(spec).validate_raw(raw, expect={"target": target, "ref": ref})
    if not ok:
        raise PatchError("MuleValidator rejected block: " + "; ".join(errs[:3]))
    try:
        return base64.b64decode(block["replacement_b64"], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise PatchError(f"replacement_b64 is not valid base64 UTF-8 ({type(e).__name__})") from None


# ---- orchestration ----------------------------------------------------------
def patch_file(
    file: str, start: int, end: int, instruction: str, *,
    url: str = DEFAULT_URL, model: str = DEFAULT_MODEL, retries: int = 3, timeout: float = 300.0,
    dry_run: bool = False, max_lines: int = MAX_LINES,
    generate: Optional[Callable[..., str]] = None, log: Callable[[str], None] = lambda s: None,
) -> Dict:
    path = Path(file).resolve()
    if not path.is_file():
        raise PatchError(f"no such file: {file}")
    lines, newline = read_lines(path)
    if not (1 <= start <= end <= len(lines)):
        raise PatchError(f"range {start}:{end} is outside the file (1..{len(lines)})")
    if end - start + 1 > max_lines:
        raise PatchError(f"slice is {end - start + 1} lines; the cap is {max_lines}. Split the edit.")

    target, ref = path.as_posix(), f"{start}:{end}"
    if not (target.isascii() and target.isprintable()) or '"' in target or "\\" in target:
        raise PatchError(f"path must be printable ASCII without quotes or backslashes: {target!r}")
    current = "".join(lines[start - 1:end])
    spec = build_slice_spec(target, ref)
    grammar = to_gbnf(spec)
    messages = build_messages(target, ref, instruction, current)
    gen = generate or request_block

    last = "no attempt made"
    for attempt in range(max(1, retries)):
        temperature = min(0.2 * attempt, 0.6)  # temperature 0 is deterministic: a retry must differ
        log(f"attempt {attempt + 1}/{retries} (temperature {temperature:.1f})")
        raw = gen(messages, grammar, url=url, model=model, temperature=temperature, timeout=timeout)
        try:
            replacement = verify_block(raw, spec, target, ref)
            new_text = apply_slice(lines, start, end, replacement, newline)
            note = syntax_check(path, new_text)
        except PatchError as e:
            last = str(e)
            log(f"  rejected: {last}")
            continue
        if not dry_run:
            write_atomic(path, new_text)
        old_text = "".join(lines)
        diff = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            "before", "after", n=1))
        return {"file": target, "ref": ref, "syntax": note, "attempts": attempt + 1,
                "diff": diff, "wrote": not dry_run}
    raise PatchError(f"all {max(1, retries)} attempts rejected; last: {last}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("start_line", type=int)
    ap.add_argument("end_line", type=int)
    ap.add_argument("instruction")
    ap.add_argument("--url", default=os.environ.get("MULE_URL", DEFAULT_URL))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--dry-run", action="store_true", help="verify and show the diff, write nothing")
    a = ap.parse_args(argv)
    try:
        r = patch_file(a.file, a.start_line, a.end_line, a.instruction, url=a.url, model=a.model,
                       retries=a.retries, timeout=a.timeout, dry_run=a.dry_run,
                       log=lambda s: print(s, file=sys.stderr))
    except PatchError as e:
        print(f"REJECTED BY MULE: {e}")
        return 1
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        print(f"MULE SERVER ERROR: cannot use {a.url} ({e}). Start it with bridge/start_vram.ps1")
        return 2
    verb = "would patch" if not r["wrote"] else "patched"
    print(r["diff"].rstrip("\n"))
    print(f"OK: {verb} {r['file']} [{r['ref']}] grammar-constrained, MuleValidator-verified, "
          f"syntax: {r['syntax']}, attempts: {r['attempts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
