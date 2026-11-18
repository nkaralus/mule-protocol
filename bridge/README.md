# Claude Code ↔ local GPU bus (Qwen worker + Gemma eyes)

Claude Code delegates bounded code edits, and screenshot/audio inspections, to local models so it
never has to load large files or media into its own context. Every message between the nodes is a
[mule-protocol](../README.md) block, forced into shape by a `to_gbnf()` grammar and checked by
`MuleValidator`.

```
 Claude Code            Gemma eyes                 Qwen worker
 10.1.0.0/24            10.2.1.0/24                10.3.1.0/24
 %Claude 10.1.0.1       %Eyes   10.2.1.1  :8081    %Worker 10.3.1.1  :8080
     │  mule_tool.py        │  (--media / inspect)     │
     └──── bus.py ──────────┴──────────────────────────┘
```

## One task = one frame through five slots

| Slot | Who | What happens |
|---|---|---|
| `SLOT_1_TASK` | Claude node | Task posted. **GI_2 scope**: file inside an allowed root, no `..`, not in `.git`, range in the file, ≤ 80 lines. |
| `SLOT_2_PLAN` | Eyes node | With `--media`: Gemma returns an **EyesReport**. Otherwise a plain plan frame. |
| `SLOT_3_EXECUTE` | Worker node | Qwen replies with one grammar-constrained block. **GI_1** echo check. |
| `SLOT_4_GUARD` | Harness | **GI_2** subnet check (a node may only speak from its own subnet and address), **GI_3** syntax check on a temp copy. Any gate failing is a **NAK**: back to slot 3 with the reason, up to **3** attempts. |
| `SLOT_5_VERIFY` | Harness | Atomic write, read-back hash check, receipt. |

Gates: **GI_1** = `MuleValidator.validate_raw(raw, expect=…)` with every pinned header slot
(`frame_id`, `time_slot`, `subnet`, `op`, `target`, `ref`, `path`, `lines`) required to be echoed
exactly, and all five standard exclusions present. **GI_2** = subnet isolation and file scope.
**GI_3** = `py_compile` / `node --check` / `json.loads`. A failed patch is never written.

## Two steps

```powershell
.\bridge\install.ps1                          # once: tool, modules, global CLAUDE.md rule
.\bridge\start_vram.ps1                       # worker-only: Qwen 14B on :8080, all layers in VRAM
```

Smoke test:

```powershell
Set-Content $env:TEMP\mule_smoke.py "x = 1`n"
python ~/tools/mule_tool.py $env:TEMP\mule_smoke.py 1 1 "Replace the line with: x = 2"
Get-Content $env:TEMP\mule_smoke.py           # expect: x = 2
```

You should see a diff, `OK: patched … attempts: 1, naks: 0, syntax: py_compile` and a `RECEIPT:` line.
`--dry-run` runs every gate and writes nothing.

## Usage

```
mule_tool.py <file> <start_line> <end_line> "<spec>" [--media <path>] [--dry-run] [--retries N]
             [--allow-root DIR] [--url WORKER_URL] [--eyes-url EYES_URL]
mule_tool.py inspect <media> "<question>"                    # eyes node only, no file edit
```

| Output | Exit | Meaning |
|---|---|---|
| `OK: patched …` + `RECEIPT: {…}` | 0 | Passed all gates, written. |
| `REJECTED BY MULE: …` + `RECEIPT: {…}` | 1 | Scope refused, or 3 NAKs. **File untouched.** |
| `MULE SERVER ERROR: worker\|eyes node …` | 2 | A node is down. Start it. |

Receipt (one line, no diff): `ok`, `frame_id`, `file`, `lines`, `slots` completed, `gates`, `attempts`,
`naks` (`gate` + reason for each), `eyes`, `wrote`, `sha`, `ms`, and `error` on failure.

**File scope.** Edits are limited to your home folder (or `MULE_ALLOWED_ROOTS`, a path-separator
list). Add more with `--allow-root DIR`. This stops a confused or manipulated worker request from
touching system files.

## Eyes node (`--media`, `inspect`)

`gemma_sensor.py` posts the media to Gemma at `http://127.0.0.1:8081/v1/chat/completions` using
`mule.prompt.build_messages(…, fold_system=True)` (Gemma has no system role, so the system prompt
is folded into the user turn) and a `to_gbnf()` grammar. Images are sent as `image_url` data URLs
(png, jpg, webp, gif), audio as `input_audio` (wav, mp3). The validated EyesReport is a mule block
with `time_slot=SLOT_2_PLAN`, `subnet=10.2.1.0/24`, `ref=10.2.1.1`, `target=%Eyes`, `op=inspect`
plus:

| Key | Type |
|---|---|
| `observations` | up to 8 strings, ≤ 200 chars each |
| `ocr_text` | printable ASCII, ≤ 500 chars. The grammar cannot carry quotes, backslashes, newlines or non-ASCII, so this is lossy. |
| `confidence` | integer 0–100 (mule blocks forbid floats) |

**Treat the report as untrusted.** Text read from a screenshot can contain instructions. The worker
is told to use it as context only, and the worker's reply is still pinned, scope-checked and
syntax-checked. It can still be steered into a wrong-but-valid edit, so read the diff.

## VRAM modes

```powershell
.\bridge\start_vram.ps1 -Mode worker-only     # Qwen only (default)
.\bridge\start_vram.ps1 -Mode concurrent      # Qwen :8080 + Gemma :8081
```

`concurrent` needs `-GemmaModel` / `-GemmaMmproj` (defaults point at
`tools\models\gemma-4-e4b-it-Q4_K_M.gguf` and `mmproj-gemma-4-e4b-it-f16.gguf`, which you must
download) and **checks the budget first**: weights + 1500 MiB per server vs the card, and refuses
before stopping anything if it does not fit. Qwen 14B Q4 is ~8.6 GiB of weights alone, so on a
12 GiB RTX 5070 `concurrent` only passes with a much smaller Qwen or Gemma quant (or `-Force`, which
risks falling back to CPU RAM). `-Reuse` keeps healthy nodes and starts only missing ones.
Logs: `bridge/logs/`.

## What is and is not guaranteed

Guaranteed: both model replies are one JSON line with the exact pinned header; the payload is valid
base64 UTF-8; the new file compiles/parses; nothing outside the allowed roots is touched; a failed
patch never reaches disk.

Not guaranteed: that the patch does what you asked. A 14B model is weak at hand-encoding base64, so
the prompt includes the slice already encoded (`CURRENT_SLICE_B64`) and NAKs feed the reason back.
A syntactically valid wrong edit still passes. Review the printed diff, then run your tests.

## Troubleshooting

- *`MULE SERVER ERROR`* → run `start_vram.ps1` (`-Mode concurrent` for the eyes node).
- *Slow or CPU-bound* → `bridge/logs/worker.err.log` should show `offloaded N/N layers to GPU`.
- *Audio rejected by the server* → `input_audio` support depends on your llama.cpp build and model.
- *Non-ASCII path* → paths must be printable ASCII without quotes or backslashes.
