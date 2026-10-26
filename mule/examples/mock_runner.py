#!/usr/bin/env python3
import json
import os
import sys

# Setup import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from mule.validator import MuleValidator
from mule.prompt import MULE_SYSTEM_PROMPT, build_mule_prompt

def main():
    spec_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../spec/mule_spec.json"))
    spec = json.load(open(spec_path))
    validator = MuleValidator(spec)

    print("=== MULE PROTOCOL STANDALONE TEST ===")
    
    # 1. Test a well-formed Mule output
    valid_sample = json.dumps({
        "frame_id": "0x2001",
        "time_slot": "SLOT_3_EXECUTE",
        "subnet": "10.1.1.0/24",
        "op": "add_wall",
        "target": "%Player",
        "ref": "10.1.1.10",
        "exclude": ["untyped_variables", "relative_paths_up_tree"]
    })
    
    ok, data, errs = validator.validate_raw(valid_sample)
    print(f"\n1. Valid Sample Test: {'PASSED' if ok else 'FAILED'}")
    if not ok:
        print("   Errors:", errs)

    # 2. Test a model hallucination (conversational text + missing slots)
    hallucinated_sample = "Sure! Here is your block:\n" + json.dumps({
        "frame_id": "invalid_id",
        "op": "add_wall"
    })
    
    ok, data, errs = validator.validate_raw(hallucinated_sample)
    print(f"\n2. Hallucinated Sample Test (Expect Failure): {'PASSED (Caught)' if not ok else 'FAILED (Missed Errors)'}")
    print("   Caught Violations:")
    for e in errs:
        print(f"    - {e}")

if __name__ == "__main__":
    main()
