# Claude Code ↔ RTX 5070 bridge

Lets Claude Code delegate bounded code edits to a local Qwen2.5-Coder-14B running fully in
GPU VRAM. Claude finds the line numbers; the local model writes the replacement; mule-protocol
guarantees the reply is well-formed before anything touches your file.

```
Claude Code ──► bridge/mule_tool.py ──► llama-server :8080 (RTX 5070, -ngl 99)
                   │  grammar = mule.constrain.to_gbnf()  (decoding is forced into the schema)
                   │  MuleValidator.validate_raw(expect={target, ref})  (echo verified)
                   │  base64 decode → syntax check on a temp copy → atomic replace
                   ▼
              your file (or untouched on any failure)
```

## Two-step startup

**1. Boot the model into VRAM**

```powershell
.\bridge\install.ps1                 # once: installs ~/tools/mule_tool.py + global CLAUDE.md rule
.\bridge\start_vram.ps1              # kills stale llama-server, loads all layers on the GPU
```

`start_vram.ps1` waits for `/health` = 200 and fails if the log does not show every layer
offloaded to the GPU. Use `-Reuse` to keep an already-healthy server instead of restarting it.
Logs go to `bridge/logs/`. The path of the runtime and model are parameters (`-RuntimeDir`, `-Model`).

**2. Smoke test**

```powershell
Set-Content $env:TEMP\mule_smoke.py "x = 1`n"
python ~/tools/mule_tool.py $env:TEMP\mule_smoke.py 1 1 "Replace the line with 'x = 2'"
Get-Content $env:TEMP\mule_smoke.py     # expect: x = 2
```

Success prints a unified diff and `OK: patched ... syntax: py_compile`. Add `--dry-run` to
verify without writing. Then check the whole repo is healthy: `python -m mule.selftest`
and `python -m pytest`.

## Usage

```
python ~/tools/mule_tool.py <file> <start_line> <end_line> "<exact edit instruction>"
```

| Result | Exit | Meaning |
|---|---|---|
| `OK: patched ...` | 0 | Validated, syntax-checked, written. |
| `REJECTED BY MULE: ...` | 1 | Every attempt failed. **The file is untouched.** |
| `MULE SERVER ERROR: ...` | 2 | Server down. Run `start_vram.ps1`. |

Options: `--retries N` (default 3; temperature rises 0 → 0.2 → 0.4 so a retry differs),
`--url`, `--timeout`, `--dry-run`. Slices are capped at 80 lines; split larger edits.

## What is guaranteed, and what is not

Guaranteed: the model output is one JSON line with exactly `target`, `ref`, `replacement_b64`
in that order; `target` and `ref` equal what the tool sent; the payload is valid base64 UTF-8;
the new file compiles (`.py`), passes `node --check` (`.js/.mjs/.cjs`) or parses (`.json`).
A failing patch is never written.

Not guaranteed: that the patch does what you asked. A syntactically valid wrong edit passes.
Review the printed diff and run your tests. Other file types are patched without a syntax
check, and if `node` is not installed the JS check is reported as skipped.

A 14B model is weak at producing base64 by hand, so rejections happen. That is the design:
the gate rejects bad output instead of writing it. To reduce them, the prompt includes the
slice already base64-encoded (`CURRENT_SLICE_B64`) so unchanged text can be copied rather than
re-encoded. Without that hint, a smoke test on a 70-character docstring line was rejected twice
and then accepted a garbled rewrite that still compiled, which is exactly why you review the
printed diff. With the hint the same edit came back correct on the first attempt.
Keep slices small and instructions exact.

## Troubleshooting

- *Connection refused* → run `start_vram.ps1`.
- *Slow or CPU-bound* → open `bridge/logs/llama-server.err.log`; you want
  `offloaded N/N layers to GPU`. 12 GB of VRAM is tight for this model plus an 8192 context.
- *Unicode in the path* → paths must be printable ASCII (the validator rejects anything else).
