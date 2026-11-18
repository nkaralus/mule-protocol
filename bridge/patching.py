"""File-level helpers for the bridge: line slices, syntax gate (GI_3), atomic write."""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import py_compile
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple


class PatchError(Exception):
    """A patch step failed. The target file has not been modified."""


def b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def read_lines(path: Path) -> Tuple[List[str], str]:
    """Lines with endings preserved, plus the file's newline style."""
    text = path.read_bytes().decode("utf-8")
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text and text.count("\r\n") > text.count("\n") / 2 else "\n"
    return lines, newline


def apply_slice(lines: List[str], start: int, end: int, replacement: str, newline: str) -> str:
    """New file text with lines start..end replaced by `replacement`."""
    replacement = replacement.replace("\r\n", "\n").replace("\n", newline)
    slice_had_eol = lines[end - 1].endswith(("\n", "\r"))
    if replacement and slice_had_eol and not replacement.endswith(newline):
        replacement += newline
    return "".join(lines[:start - 1]) + replacement + "".join(lines[end:])


def syntax_check(path: Path, text: str) -> str:
    """GI_3. Check `text` as it would be saved at `path`. Returns a short note; raises PatchError."""
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


def unified_diff(old: str, new: str, limit: int = 60) -> str:
    out = list(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                    "before", "after", n=1))
    if len(out) > limit:
        out = out[:limit] + [f"... diff truncated ({len(out) - limit} more lines)\n"]
    return "".join(out)
