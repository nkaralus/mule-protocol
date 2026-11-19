"""Completion gates: a task is done only when its declared files are real and its runtime is wired.

A completion frame is a strict mule block (SLOT_5_VERIFY, Claude node, op `verify`) that declares what
the finished task must have left on disk. It is checked against the files, never against a model's
word, so "tests pass" or "deferred to the user" cannot stand in for the work.

    GI_1  MuleValidator on the frame (header pinned, additionalProperties false, no backslashes).
    GI_4  Artifact / reference extraction completeness:
            - `skipped` must be empty: declared work cannot be deferred or left uncopied;
            - every `artifacts` path exists and is not a placeholder or stub;
            - every `copies` entry "source>target" has its source, and target is a byte-for-byte copy.
    GI_5  Dead wiring / stub-only tests:
            - every `runtime` file changed since commit `base` (committed, staged, unstaged or new);
            - a change set that touches only tests while `runtime` files were declared is refused;
            - every `hooks` entry "path::needle" finds `needle` in `path` outside comments.
          `tests_passed: true` never overrides a GI_4 or GI_5 failure.

Paths are relative to the root (POSIX, no spaces, no `..`). Stub detection is deterministic: empty
files, stub markers (TODO, FIXME, placeholder, stub, TBD, not implemented, deferred to user, ...),
Python functions whose body is only pass / ... / raise NotImplementedError, empty JS function
declarations, empty JSON containers, and PNG images of 2x2 pixels or smaller.
"""
from __future__ import annotations

import ast
import io
import json
import re
import struct
import subprocess
import tokenize
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from bus import NODES, Nak, frame, gi1_echo, node_spec

PATH = "[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}"
NEEDLE = "[ -~]{1,160}"
MAX_ITEMS = 64
MAX_ERRORS = 12

STUB_RE = re.compile(r"\b(todo|fixme|placeholder|stub|stubbed|tbd|lorem ipsum|not implemented|"
                     r"notimplementederror|deferred to (?:the )?user|left (?:for|to) the user)\b", re.I)
EMPTY_JS_FN = re.compile(r"\bfunction\s*\*?\s*[A-Za-z_$][\w$]*\s*\([^)]*\)\s*\{\s*\}")
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".json", ".md", ".txt", ".lang", ".mcfunction",
                 ".html", ".css", ".yml", ".yaml", ".toml", ".ps1", ".sh", ".cfg", ".ini"}
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")


# ---- frame ------------------------------------------------------------------
def _arr(pattern: str, max_len: int) -> Dict[str, Any]:
    return {"type": "array", "items": {"type": "string", "pattern": pattern, "maxLength": max_len},
            "maxItems": MAX_ITEMS, "uniqueItems": True}


def completion_spec(frame_id: str) -> Dict[str, Any]:
    extra = {
        "base": {"type": "string", "pattern": "[0-9a-f]{7,40}", "maxLength": 40},
        "artifacts": _arr(PATH, 200),
        "copies": _arr(f"{PATH}>{PATH}", 401),
        "runtime": _arr(PATH, 200),
        "hooks": _arr(f"{PATH}::{NEEDLE}", 362),
        "skipped": _arr(NEEDLE, 160),
        "tests_passed": {"type": "boolean"},
    }
    return node_spec(NODES["claude"], "SLOT_5_VERIFY", "verify", frame_id, extra, max_raw=65536)


def completion_frame(frame_id: str, *, base: str, artifacts: Sequence[str] = (), copies: Sequence[str] = (),
                     runtime: Sequence[str] = (), hooks: Sequence[str] = (), skipped: Sequence[str] = (),
                     tests_passed: bool = False) -> Dict[str, Any]:
    return frame(NODES["claude"], "SLOT_5_VERIFY", "verify", frame_id, base=base, artifacts=list(artifacts),
                 copies=list(copies), runtime=list(runtime), hooks=list(hooks), skipped=list(skipped),
                 tests_passed=tests_passed)


def gi1_frame(raw: str) -> Dict[str, Any]:
    """GI_1 for a completion frame. The frame_id is read first only to pin it for the echo check."""
    try:
        fid = json.loads(raw).get("frame_id")
    except (ValueError, AttributeError):
        fid = None
    if not isinstance(fid, str) or not re.fullmatch(r"0x[0-9a-fA-F]{4}", fid):
        raise Nak("GI_1", "completion frame is not a JSON object with a valid frame_id", retry=False)
    return gi1_echo(raw, completion_spec(fid))


# ---- stub detection -----------------------------------------------------------
def _resolve(root: Path, rel: str) -> Path:
    if ".." in rel.split("/") or rel.startswith("/"):
        raise ValueError(f"{rel}: path climbs the tree (relative_paths_up_tree)")
    return root / rel


def _py_stub_functions(text: str) -> List[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]   # docstring
            if all(_is_noop(s) for s in body):
                out.append(node.name)
    return out


def _is_noop(s: ast.stmt) -> bool:
    if isinstance(s, ast.Pass):
        return True
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis:
        return True
    if isinstance(s, ast.Raise) and s.exc is not None:
        exc = s.exc.func if isinstance(s.exc, ast.Call) else s.exc
        return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
    return False


def stub_reason(path: Path) -> Optional[str]:
    """Why `path` is a placeholder or stub, or None if it looks real. The file must exist."""
    data = path.read_bytes()
    if not data.strip():
        return "is empty"
    suffix = path.suffix.lower()
    if suffix == ".png":
        if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return "is not a valid PNG"
        w, h = struct.unpack(">II", data[16:24])
        return f"is a {w}x{h} placeholder image" if w * h <= 4 else None
    if suffix in (".jpg", ".jpeg"):
        return None if data[:3] == b"\xff\xd8\xff" else "is not a valid JPEG"
    if suffix not in TEXT_SUFFIXES:
        return None
    text = data.decode("utf-8", "replace")
    m = STUB_RE.search(text)
    if m:
        return f"contains stub marker {m.group(0)!r}"
    if suffix == ".json" and text.strip() in ("{}", "[]", "null", '""'):
        return "is an empty JSON placeholder"
    if suffix == ".py":
        fns = _py_stub_functions(text)
        if fns:
            return f"has stub function(s) {', '.join(fns[:5])} (body is only pass / ... / NotImplementedError)"
    if suffix in (".js", ".mjs", ".cjs", ".ts"):
        m = EMPTY_JS_FN.search(_strip_comments(text, suffix))
        if m:
            return f"has an empty function {m.group(0)!r}"
    return None


def _strip_comments(text: str, suffix: str) -> str:
    if suffix == ".py":
        try:
            toks = [t for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type != tokenize.COMMENT]
            return tokenize.untokenize(toks)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return re.sub(r"#[^\n]*", "", text)
    if suffix in (".js", ".mjs", ".cjs", ".ts"):
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        return re.sub(r"(?<![:\\])//[^\n]*", "", text)
    return text


# ---- GI_4 -------------------------------------------------------------------
def gi4_artifacts(block: Dict[str, Any], root: Path) -> List[str]:
    """Problems with declared artifacts and copies. Empty list = GI_4 passes."""
    errs: List[str] = []
    for item in block["skipped"]:
        errs.append(f"declared work was skipped / deferred: {item!r}")
    for rel in block["artifacts"]:
        errs += _artifact(root, rel, "declared artifact")
    for pair in block["copies"]:
        src_rel, dst_rel = pair.split(">", 1)
        try:
            src, dst = _resolve(root, src_rel), _resolve(root, dst_rel)
        except ValueError as e:
            errs.append(str(e))
            continue
        if not src.is_file():
            errs.append(f"source asset {src_rel} does not exist")
            continue
        if not dst.is_file():
            errs.append(f"{src_rel} was not copied to {dst_rel} (uncopied)")
            continue
        if src.read_bytes() != dst.read_bytes():
            errs.append(f"{dst_rel} is not a copy of {src_rel} ({dst.stat().st_size} vs {src.stat().st_size} bytes)")
            continue
        errs += _artifact(root, dst_rel, "copied asset")
    return errs


def _artifact(root: Path, rel: str, what: str) -> List[str]:
    try:
        p = _resolve(root, rel)
    except ValueError as e:
        return [str(e)]
    if not p.is_file():
        return [f"{what} {rel} was not produced"]
    why = stub_reason(p)
    return [f"{what} {rel} {why}"] if why else []


# ---- GI_5 -------------------------------------------------------------------
def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)


def changed_files(root: Path, base: str) -> List[str]:
    """Files under root that differ from `base` (committed since, staged, unstaged) plus untracked ones."""
    r = _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if r.returncode != 0:
        raise ValueError(f"base {base} is not a commit in {root.as_posix()}")
    diff = _git(root, "diff", "--name-only", "--relative", base, "--")
    new = _git(root, "ls-files", "--others", "--exclude-standard")
    if diff.returncode != 0 or new.returncode != 0:
        raise ValueError(f"git failed in {root.as_posix()}: {(diff.stderr or new.stderr).strip()[:160]}")
    return sorted({ln.strip() for ln in (diff.stdout + "\n" + new.stdout).splitlines() if ln.strip()})


def gi5_wiring(block: Dict[str, Any], root: Path) -> List[str]:
    """Problems with runtime changes and hooks. Empty list = GI_5 passes."""
    errs: List[str] = []
    runtime, hooks = block["runtime"], block["hooks"]
    if runtime:
        try:
            changed = set(changed_files(root, block["base"]))
        except ValueError as e:
            return [str(e)]
        if changed and all(TEST_PATH.search(c) for c in changed):
            errs.append(f"only test files changed since {block['base'][:12]} ({', '.join(sorted(changed)[:5])}): "
                        "stub-only tests do not complete a task")
        for rel in runtime:
            try:
                p = _resolve(root, rel)
            except ValueError as e:
                errs.append(str(e))
                continue
            if not p.is_file():
                errs.append(f"runtime file {rel} does not exist")
            elif rel not in changed:
                errs.append(f"runtime file {rel} was not modified since {block['base'][:12]}")
            else:
                why = stub_reason(p)
                if why:
                    errs.append(f"runtime file {rel} {why}")
    for hook in hooks:
        rel, needle = hook.split("::", 1)
        try:
            p = _resolve(root, rel)
        except ValueError as e:
            errs.append(str(e))
            continue
        if not p.is_file():
            errs.append(f"hook file {rel} does not exist (needs {needle!r})")
        elif needle not in _strip_comments(p.read_text(encoding="utf-8", errors="replace"), p.suffix.lower()):
            errs.append(f"required runtime hook {needle!r} is missing from {rel} (dead wiring)")
    return errs


# ---- run --------------------------------------------------------------------
def verify_completion(raw: str, root: Path) -> Dict[str, Any]:
    """Run GI_1, GI_4 and GI_5. Returns a receipt; `ok` is True only if every gate passed."""
    root = Path(root).resolve()
    rec: Dict[str, Any] = {"ok": False, "frame_id": None, "root": root.as_posix(), "gates": {}, "errors": []}
    try:
        block = gi1_frame(raw)
    except Nak as n:
        rec["gates"]["GI_1"] = "fail"
        rec["errors"] = [str(n)]
        return rec
    rec["frame_id"] = block["frame_id"]
    rec["gates"]["GI_1"] = "echo ok"
    suffix = " (tests_passed=true does not override this)" if block["tests_passed"] else ""
    for gate, fn in (("GI_4", gi4_artifacts), ("GI_5", gi5_wiring)):
        errs = fn(block, root)
        rec["gates"][gate] = "fail" if errs else "ok"
        rec["errors"] += [f"{gate}: {e}{suffix}" for e in errs]
    rec["errors"] = rec["errors"][:MAX_ERRORS]
    rec["ok"] = not rec["errors"]
    return rec


def receipt_json(rec: Dict[str, Any]) -> str:
    return json.dumps(rec, separators=(",", ":"))
