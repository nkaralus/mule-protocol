# mule-protocol

Make a local 14B-32B model fill a fixed slot template, and **deterministically reject anything else**.

```
  model text ──► constrained decoding ──► MuleValidator ──► block | rejection
                  (layer 1: grammar)       (layer 2: authority)
```

Model output is not deterministic. Validation is. Nothing leaves `validate_raw` unless it passes.

## Quickstart
```bash
pip install -e ".[dev]"
python -m mule.selftest        # no GPU needed
pytest
```
```python
from mule import MuleValidator
from mule.constrain import to_json_schema, to_gbnf
from mule.prompt import build_messages

ok, block, errors = MuleValidator().validate_raw(model_text)
```
`block` is `{}` unless `ok`. Pass `expect={"target": ..., "ref": ...}` to force the model to echo harness values.

## What gets rejected
Markdown fences, preamble/trailing text, `<think>` blocks, newlines/tabs, non-ASCII, backslash escapes,
duplicate keys, extra keys, NaN/Infinity/floats, bool-as-int, bad IPs (leading zeros, octets > 255),
`ref` outside `subnet`, oversize input.

## Runtimes
| Backend | Constraint | Helper |
|---|---|---|
| Ollama | `format` = JSON schema | `build_payload("ollama", ...)` |
| vLLM / LM Studio | `response_format` json_schema | `build_payload("openai", ...)` |
| llama.cpp server | `grammar` = GBNF | `build_payload("llamacpp", ...)` |

Gemma has no system role: use `build_messages(..., fold_system=True)`.
Qwen3: thinking is switched off in the payload; the validator rejects `<think>` anyway.

## Scope
Syntax only. Registry existence and subnet isolation live in `engine-telemetry-bus`.
