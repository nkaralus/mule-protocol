import json
import random

import pytest

from mule import MuleValidator, load_spec
from mule.selftest import BAD, GOOD_RAW, _DROP, _o, good_block

V = MuleValidator()


def test_good_block():
    ok, block, errs = V.validate_raw(GOOD_RAW)
    assert ok, errs
    assert block == good_block()


@pytest.mark.parametrize("name,raw", BAD, ids=[n for n, _ in BAD])
def test_bad_blocks_rejected(name, raw):
    ok, block, errs = V.validate_raw(raw)
    assert not ok
    assert block == {}          # nothing unvalidated escapes
    assert errs


def test_error_text_is_ascii_and_bounded():
    _, _, errs = V.validate_raw(_o(target="%" + "\u202e" * 500))
    assert all(e.isascii() and len(e) < 300 for e in errs)


def test_expect_pins_harness_values():
    ok, _, errs = V.validate_raw(GOOD_RAW, expect={"target": "%Player", "ref": "10.1.1.10"})
    assert ok, errs
    ok, block, errs = V.validate_raw(GOOD_RAW, expect={"target": "%Wall"})
    assert not ok and block == {}
    with pytest.raises(ValueError):
        V.validate_raw(GOOD_RAW, expect={"nope": 1})


def test_require_standard_exclusions():
    strict = MuleValidator(require_standard_exclusions=True)
    assert not strict.validate_raw(GOOD_RAW)[0]
    full = _o(exclude=load_spec()["standard_exclusions"])
    assert strict.validate_raw(full)[0]


def test_allowed_ops():
    v = MuleValidator(allowed_ops={"bind_move"})
    assert v.validate_raw(GOOD_RAW)[0]
    assert not v.validate_raw(_o(op="delete_all"))[0]


def test_tolerate_edge_whitespace_is_opt_in():
    assert not V.validate_raw(GOOD_RAW + "\n")[0]
    assert MuleValidator(tolerate_edge_whitespace=True).validate_raw("\n" + GOOD_RAW + "\n")[0]
    # interior whitespace is never tolerated
    assert not MuleValidator(tolerate_edge_whitespace=True).validate_raw(GOOD_RAW.replace(",", ",\n", 1))[0]


def test_extension_slots():
    ext = {"speed": {"type": "integer", "minimum": 0, "maximum": 1000}}
    v = MuleValidator(extensions=ext)
    raw = _o(speed=200)
    assert v.validate_raw(raw)[0]
    assert not v.validate_raw(_o(speed=True))[0]      # bool is not an int here
    assert not v.validate_raw(_o(speed=5000))[0]
    assert not v.validate_raw(GOOD_RAW)[0]            # missing extension slot
    with pytest.raises(ValueError):
        MuleValidator(extensions={"op": {"type": "string"}})


def test_bad_spec_is_rejected_at_construction():
    spec = load_spec()
    spec["slots"]["op"]["pattren"] = "x"              # typo must not silently disable a rule
    with pytest.raises(ValueError):
        MuleValidator(spec)
    spec = load_spec()
    spec["additionalProperties"] = True
    with pytest.raises(ValueError):
        MuleValidator(spec)


def test_mutation_fuzz_never_accepts_unsafe_output():
    rng = random.Random(0)
    alphabet = ['"', "\\", "\n", "\t", "\x00", "\u202e", "\u200b", "\u0420", "`", "{", "}", "[", "]",
                ",", ":", "%", ".", "/", " ", "0", "9", "e", "-", "N", "\uff11"]
    accepted = 0
    for _ in range(4000):
        s = list(GOOD_RAW)
        for _ in range(rng.randint(1, 3)):
            pos = rng.randrange(len(s) + 1)
            kind = rng.choice(["ins", "del", "sub"])
            if kind == "ins":
                s.insert(pos, rng.choice(alphabet))
            elif kind == "del" and s:
                s.pop(min(pos, len(s) - 1))
            elif s:
                s[min(pos, len(s) - 1)] = rng.choice(alphabet)
        raw = "".join(s)
        ok, block, _ = V.validate_raw(raw)
        if ok:
            accepted += 1
            assert raw.isascii() and raw.isprintable()
            assert list(block) == list(load_spec()["slots"]) or set(block) == set(load_spec()["slots"])
            assert json.loads(raw) == block
    assert accepted < 4000
