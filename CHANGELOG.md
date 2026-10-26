# Changelog

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
