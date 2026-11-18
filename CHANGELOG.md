# Changelog

## Unreleased
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
