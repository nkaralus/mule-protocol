import json
import random

import pytest

from mule import MuleValidator, load_spec
from mule.constrain import regex_to_gbnf, to_gbnf, to_json_schema
from mule.prompt import EXAMPLE, _example_block, build_messages, build_state
from mule.runners import build_payload, endpoint
from mule.selftest import BAD, GOOD_RAW

V = MuleValidator()


def _all_patterns(rules):
    if "pattern" in rules:
        yield rules["pattern"]
    if "items" in rules:
        yield from _all_patterns(rules["items"])


def test_every_spec_pattern_is_in_the_grammar_subset():
    for name, rules in load_spec()["slots"].items():
        for p in _all_patterns(rules):
            regex_to_gbnf(p)


@pytest.mark.parametrize("bad", ["a|b", "(ab)", r"\d", ".", "^a", "a$", "[a\\]b]", "[abc"])
def test_regex_subset_rejects_unsupported(bad):
    with pytest.raises(ValueError):
        regex_to_gbnf(bad)


def test_regex_translation_examples():
    assert regex_to_gbnf(r"0x[0-9a-fA-F]{4}") == '"0x" [0-9a-fA-F]{4}'
    assert regex_to_gbnf(r"10\.[0-9]{1,3}\.0/24") == '"10." [0-9]{1,3} ".0/24"'
    assert regex_to_gbnf(r"%[A-Z][a-zA-Z0-9]{0,63}") == '"%" [A-Z] [a-zA-Z0-9]{0,63}'


def test_gbnf_shape():
    g = to_gbnf()
    assert g.startswith('root ::= "{" "\\"frame_id\\":" frame-id ')
    for rule in ("frame-id", "time-slot", "subnet", "op", "target", "ref", "exclude", "exclude-item"):
        assert f"\n{rule} ::= " in g
    assert "_" not in "".join(l.split(" ::=")[0] for l in g.splitlines())  # GBNF names: no underscores
    assert '"SLOT_3_EXECUTE"' in g


def test_schema_shape():
    s = to_json_schema()
    assert s["additionalProperties"] is False
    assert s["required"] == list(load_spec()["slots"])
    assert s["properties"]["frame_id"]["pattern"] == "^0x[0-9a-fA-F]{4}$"
    assert s["properties"]["exclude"]["uniqueItems"] is True


def test_validator_accepts_only_what_schema_accepts():
    jsonschema = pytest.importorskip("jsonschema")
    schema = to_json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    check = jsonschema.Draft202012Validator(schema)
    assert check.is_valid(json.loads(GOOD_RAW))
    rng = random.Random(1)
    for _ in range(3000):
        raw = list(GOOD_RAW)
        for _ in range(rng.randint(1, 3)):
            i = rng.randrange(len(raw))
            raw[i] = rng.choice("0123456789abcdefXYZ%./_ \"[],:-")
        raw = "".join(raw)
        ok, block, _ = V.validate_raw(raw)
        if ok:
            assert check.is_valid(block), raw   # validator must be at least as strict as the schema


def test_prompt_example_passes_validator():
    spec = load_spec()
    ok, _, errs = MuleValidator(require_standard_exclusions=True).validate_raw(_example_block(spec))
    assert ok, errs


def test_messages_shapes():
    kw = dict(frame_id="0x0002", subnet="10.1.1.0/24", op="bind_move", target="%Wall", ref="10.1.1.14")
    m = build_messages("test", **kw)
    assert [x["role"] for x in m] == ["system", "user", "assistant", "user"]
    f = build_messages("test", fold_system=True, **kw)
    assert [x["role"] for x in f] == ["user", "assistant", "user"]
    assert f[0]["content"].startswith("You are a slot-filling component")
    z = build_messages("test", few_shot=False, **kw)
    assert [x["role"] for x in z] == ["system", "user"]


def test_state_is_json_not_python_repr():
    s = build_state("it's \u00e9", **{k: v for k, v in EXAMPLE.items() if k != "intent"})
    body = s.split("STATE: ", 1)[1].rsplit("\nOUTPUT:", 1)[0]
    parsed = json.loads(body)          # the original code emitted a Python dict repr (single quotes)
    assert parsed["intent"] == "it's \u00e9"
    assert s.isascii()
    with pytest.raises(ValueError):
        build_state("x" * 501, **{k: v for k, v in EXAMPLE.items() if k != "intent"})


def test_payload_builders():
    msgs = [{"role": "user", "content": "x"}]
    schema, grammar = to_json_schema(), to_gbnf()
    o = build_payload("ollama", "m", msgs, schema=schema)
    assert o["format"] == schema and o["think"] is False and o["options"]["temperature"] == 0
    a = build_payload("openai", "m", msgs, schema=schema)
    assert a["response_format"]["json_schema"]["schema"] == schema and a["stop"] == ["\n"]
    l = build_payload("llamacpp", "m", msgs, grammar=grammar)
    assert l["grammar"] == grammar and "response_format" not in l
    assert endpoint("ollama").endswith("/api/chat")
    with pytest.raises(ValueError):
        build_payload("nope", "m", msgs)
