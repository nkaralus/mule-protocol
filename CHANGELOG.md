# Changelog

## Unreleased
- `start_vram.ps1`: `-Mode sequential` (one model in VRAM at a time; `-Phase eyes|worker`, called by the bus
  before each slot) and `-Mode overnight-26b` (one Gemma 4 26B A4B MoE server for both roles, hot experts in
  VRAM via `--n-cpu-moe` / `-OverrideTensor`; needs a GGUF). `-DryRun` plans without touching anything.
  The chosen mode is saved to `~/.mule/mode.json`; `mule_tool.py` reads URLs and swap behaviour from it.
- `bus.py` / `mule_tool.py`: persistent DLQ (`bridge/dlq.jsonl`) for 3-strike NAK and invalid-eyes failures,
  `mule_tool.py status` and `dlq`, node swaps for sequential mode. Global CLAUDE.md rule: delegate media when
  the sensor is up or on-demand, inspect directly only when it is down.
- `bridge/` is now a 3-node TDMA bus: Claude 10.1.0.0/24, Gemma eyes 10.2.1.0/24 (`gemma_sensor.py`,
  `--media` / `inspect`), Qwen worker 10.3.1.0/24. Five slots (`SLOT_1_TASK` .. `SLOT_5_VERIFY`), gates
  GI_1 echo / GI_2 subnet+file scope / GI_3 syntax, up to 3 NAK retries, compact JSON receipt.
  `start_vram.ps1 -Mode worker-only|concurrent` with a VRAM budget preflight. Tests: `tests/test_bridge_bus.py`.
- `bridge/`: Claude Code + local GPU worker bridge. `start_vram.ps1` (all layers in VRAM, waits on
  /health), `mule_tool.py` (line-slice patcher: `to_gbnf()` grammar, `MuleValidator` target/ref echo
  check, base64 payload, syntax check on a temp copy, atomic write), `install.ps1` (idempotent
  install of the tool and the global delegation rule), README. Tests in `tests/test_bridge.py`.

## 0.2.0 - spec 1.1.0
Security hardening.
- Validator: reject extra keys, duplicate keys, NaN/Infinity, floats, fences, non-ASCII,
  control characters, backslash escapes, bool-as-int, oversize input. Full-match patterns.
  IPs checked with `ipaddress`. `ref` must sit inside `subnet`. Returns `{}` unless valid.
- Spec: `op` constrained, `exclude` items constrained, length caps, `uniqueItems`.
  Spec is no longer labelled as JSON Schema (it is not); a real schema is generated.
- Prompt: state is now JSON (was a Python dict repr). Removed "use the registered default".
  Prompt generated from the spec. Gemma-style system folding.
- New: `constrain.py` (JSON Schema + GBNF), `runners.py`, `expect=` echo check, pytest suite.

## 0.1.0
Initial scaffold.
