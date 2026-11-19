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
| `SLOT_3_EXECUTE` | Worker node | Qwen replies with one grammar-constrained header block, then the replacement as plain code between `<<<REPLACEMENT` and `REPLACEMENT>>>` lines. **GI_1** envelope + echo check. |
| `SLOT_4_GUARD` | Harness | **GI_2** subnet check (a node may only speak from its own subnet and address), **GI_3** syntax check on a temp copy. Any gate failing is a **NAK**: back to slot 3 with the reason, up to **3** attempts. |
| `SLOT_5_VERIFY` | Harness | Atomic write, read-back hash check, receipt. |

Gates: **GI_1** = `MuleValidator.validate_raw(raw, expect=…)` with every pinned header slot
(`frame_id`, `time_slot`, `subnet`, `op`, `target`, `ref`, `path`, `lines`) required to be echoed
exactly, and all five standard exclusions present. **GI_2** = subnet isolation and file scope.
**GI_3** = `py_compile` / `node --check` / `json.loads`. A failed patch is never written.

Code never travels inside a mule block (`freeform_code_blocks`): the header block carries no payload,
and the replacement follows it as raw text. The payload used to be `replacement_b64` inside the
block, which a 14B model cannot encode reliably (every patch NAKed on GI_1/GI_3). The worker request
also sends `stop: []`, because the runner's default `"\n"` stop would cut the reply after the header.

## Completion gates (GI_4 / GI_5)

`python ~/tools/mule_tool.py complete <frame.json> --root <repo>` checks a finished task against the
files, not against anyone's word. The frame is a strict mule block (`SLOT_5_VERIFY`, Claude node, op
`verify`, no unknown keys, no backslashes) built with `completion.completion_frame(...)`:

```json
{"frame_id":"0x0c0d","time_slot":"SLOT_5_VERIFY","subnet":"10.1.0.0/24","op":"verify","target":"%Claude",
 "ref":"10.1.0.1","exclude":["..."],"base":"<commit sha before the task>",
 "artifacts":["scripts/car.js"],"copies":["ref/car.png>RP/textures/car.png"],
 "runtime":["scripts/main.js"],"hooks":["scripts/main.js::REGISTRATIONS = [registerCar]"],
 "skipped":[],"tests_passed":true}
```

- **GI_4 artifacts / extraction completeness**: `skipped` must be empty (nothing "deferred to user");
  every `artifacts` file exists and is not a stub; every `copies` source exists and its target is a
  byte-for-byte copy (uncopied or replaced-by-placeholder fails). Stubs: empty files, markers (TODO,
  FIXME, placeholder, stub, TBD, not implemented, deferred to user), Python functions that only
  `pass` / `...` / `raise NotImplementedError`, empty JS function declarations, `{}` / `[]` JSON,
  PNGs of 2x2 pixels or less.
- **GI_5 dead wiring / stub-only tests**: every `runtime` file changed since `base` (committed,
  staged, unstaged or new) and is not a stub; a change set of only test files is refused; every
  `hooks` needle is present in its file outside comments. `tests_passed: true` overrides nothing.

Paths are relative POSIX without spaces or `..`; hook needles cannot contain `"` or `\` (use single
quotes). Exit 0 = complete, 1 = rejected (each reason printed, then a `RECEIPT:` line).

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
.\bridge\start_vram.ps1 -Mode worker-only     # Qwen 14B :8080 (default)
.\bridge\start_vram.ps1 -Mode sequential      # day shift: one model in VRAM at a time
.\bridge\start_vram.ps1 -Mode concurrent      # Qwen :8080 + Gemma E4B :8081 (does not fit on 12 GiB)
.\bridge\start_vram.ps1 -Mode overnight-26b   # night shift: Gemma 4 26B A4B for both roles (needs a GGUF)
```

Every mode runs a **preflight before stopping anything** (files exist, VRAM/RAM budget), stops stale
`llama-server` processes, launches the Vulkan build with `-ngl 99`, waits for `/health`, and checks
the weights really went to the GPU (the `offloaded N/N` log line, or, on builds that log quietly, VRAM
growing by about the model size). `-DryRun` prints the plan and changes nothing. `-Reuse` keeps healthy
nodes. `-Force` skips the budget refusal. Logs: `bridge/logs/`. The chosen mode is saved to
`~/.mule/mode.json` and `mule_tool.py` reads the node URLs and swap behaviour from it.

**sequential (day shift).** Gemma E4B (`gemma-4-E4B-it-Q4_0.gguf` + `mmproj-gemma-4-E4B-it-BF16.gguf`,
~6.7 GiB with overhead) and Qwen (~9.8 GiB) never share the card. `-Phase eyes` boots Gemma on :8081,
`-Phase worker` stops it and boots Qwen on :8080. You do not call these yourself: with `--media` the bus
runs `eyes` before `SLOT_2_PLAN` and `worker` before `SLOT_3_EXECUTE`, and a plain edit just ensures the
worker. A swap takes a few seconds (a full media edit measured 25 s end to end). Phases are idempotent.

**concurrent.** Qwen alone is ~8.4 GiB of weights; with Gemma E4B and a 1500 MiB allowance per server the
estimate is 16,897 MiB (~16.5 GiB) against the card's 12,227 MiB, so it refuses. Use sequential.

**overnight-26b (night shift).** `C:\mule_models\gemma26b` is a Gemma 4 26B A4B MoE (30 layers, 128
experts, top-8) in **safetensors**, `gemma26b-split` is the same weights in 29 shards, and
`gemma26b-int8experts` is a custom `layer_*.bin` format. llama-server loads only GGUF, so none of them
can be launched as they are. The script expects a GGUF and mmproj (`-Big26bModel`, `-Big26bMmproj`;
defaults under `C:\mule_models\gemma26b-gguf\`) and prints the exact `convert_hf_to_gguf.py` +
`llama-quantize` steps if they are missing. Once present it runs **one** server on :8080 that serves both
roles (the bus is pointed at it for eyes too, since two copies of a 26B cannot fit in 16 GiB of RAM).
Hot experts stay in VRAM: `--n-cpu-moe N` keeps the experts of the first N layers in RAM
(N = 30 - `-HotExpertLayers`, default 8 hot), or pass your own `-OverrideTensor` pattern. The budget
check estimates VRAM and RAM and is tight on this machine; treat it as a guide, not a guarantee.

## DLQ (`bridge/dlq.jsonl`)

A task that uses up its NAK retries (3 by default), or gets an invalid eyes report, is appended as one JSON
line: timestamp, kind (`worker_naks` / `eyes_invalid`), frame id, file, line range, the instruction (first
300 chars), media file name, every NAK gate and reason, and the mode. File contents are never copied. It
is gitignored and local. `python ~/tools/mule_tool.py dlq --tail 20` lists it. `MULE_DLQ` overrides the path.

## Status and media rule

`python ~/tools/mule_tool.py status` prints `STATUS: {"mode", "worker", "eyes", ...}` where each node is
`up`, `down`, or `on-demand` (sequential mode boots it when needed). The global CLAUDE.md rule uses it:
delegate media to `inspect` / `--media` whenever the eyes node is `up` or `on-demand`, and only inspect an
image directly when it is `down`, saying that the local sensor was offline.

## What is and is not guaranteed

Guaranteed: both model replies start with one JSON line with the exact pinned header; the worker's
replacement is delimited, NUL-free and at most 12288 characters; the new file compiles/parses;
nothing outside the allowed roots is touched; a failed patch never reaches disk.

Not guaranteed: that the patch does what you asked. NAKs feed the reason back to the worker, but a
syntactically valid wrong edit still passes. Review the printed diff, then run your tests. GI_4/GI_5
catch skipped, stubbed and unwired work; they cannot tell whether real-looking code is correct.

## Troubleshooting

- *`MULE SERVER ERROR`* → run `start_vram.ps1` (`-Mode concurrent` for the eyes node).
- *Slow or CPU-bound* → `bridge/logs/worker.err.log` should show `offloaded N/N layers to GPU`.
- *Audio rejected by the server* → `input_audio` support depends on your llama.cpp build and model.
- *Non-ASCII path* → paths must be printable ASCII without quotes or backslashes.
