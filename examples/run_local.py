"""Send one task to a local runtime and validate the reply.

  python examples/run_local.py --backend ollama   --model qwen3:32b
  python examples/run_local.py --backend openai   --model Qwen/Qwen3-32B --base-url http://localhost:8000
  python examples/run_local.py --backend llamacpp --model any
Add --fold-system for Gemma-style templates with no system role.
"""
import argparse
import sys

from mule import MuleValidator
from mule.constrain import to_gbnf, to_json_schema
from mule.prompt import build_messages
from mule.runners import generate

p = argparse.ArgumentParser()
p.add_argument("--backend", choices=["ollama", "openai", "llamacpp"], required=True)
p.add_argument("--model", required=True)
p.add_argument("--base-url")
p.add_argument("--fold-system", action="store_true")
p.add_argument("--intent", default="bind arrow-key movement to the player")
a = p.parse_args()

task = dict(frame_id="0x0001", subnet="10.1.1.0/24", op="bind_move", target="%Player", ref="10.1.1.10")
msgs = build_messages(a.intent, fold_system=a.fold_system, **task)
raw = generate(a.backend, a.model, msgs, base_url=a.base_url, schema=to_json_schema(), grammar=to_gbnf())
ok, block, errs = MuleValidator(require_standard_exclusions=True).validate_raw(
    raw.strip(), expect={k: v for k, v in task.items()})
print("RAW:", raw)
print("OK" if ok else "REJECTED", errs or block)
sys.exit(0 if ok else 1)
