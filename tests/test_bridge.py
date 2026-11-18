"""bridge/mule_tool.py: zero-GPU tests. The model is replaced by a fake `generate`."""
import base64
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from mule import MuleValidator
from mule.constrain import to_gbnf

_SPEC = importlib.util.spec_from_file_location(
    "mule_tool", Path(__file__).resolve().parent.parent / "bridge" / "mule_tool.py")
mt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mt)


def block(target, ref, text, **over):
    b = {"target": target, "ref": ref, "replacement_b64": base64.b64encode(text.encode()).decode()}
    b.update(over)
    return json.dumps(b, separators=(",", ":"))


def fake(*outputs):
    """generate() stand-in that returns the given raw outputs in turn (a callable may build one)."""
    it = iter(outputs)

    def gen(messages, grammar, **kw):
        nxt = next(it)
        return nxt(messages, grammar, **kw) if callable(nxt) else nxt
    gen.calls = it
    return gen


def run(path, start, end, gen, **kw):
    """patch_file with a fake model that answers for whatever target/ref it was given."""
    return mt.patch_file(str(path), start, end, "do it", generate=gen, retries=kw.pop("retries", 1), **kw)


def answer(text, **over):
    def build(messages, grammar, **kw):
        u = messages[-1]["content"]
        target = u.split("target=", 1)[1].split("\n", 1)[0]
        ref = u.split("ref=", 1)[1].split("\n", 1)[0]
        return block(target, ref, text, **over)
    return build


@pytest.fixture
def py(tmp_path):
    p = tmp_path / "m.py"
    p.write_text('"""doc"""\nimport os\nx = 1\n', encoding="utf-8")
    return p


# ---- grammar / validator ----------------------------------------------------
def test_grammar_pins_target_and_ref_and_bounds_base64():
    spec = mt.build_slice_spec("C:/a/b.py", "3:5")
    g = to_gbnf(spec)
    assert g.startswith('root ::= "{" "\\"target\\":" target "," "\\"ref\\":" ref "," "\\"replacement_b64\\":" replacement-b64 "}"')
    assert 'target ::= "\\"" ("C:/a/b.py") "\\""' in g
    assert 'ref ::= "\\"" ("3:5") "\\""' in g
    assert "[A-Za-z0-9+/=]*" in g


def test_validator_gate_accepts_good_and_rejects_bad_echo():
    spec = mt.build_slice_spec("C:/a/b.py", "3:5")
    assert mt.verify_block(block("C:/a/b.py", "3:5", "hi\n"), spec, "C:/a/b.py", "3:5") == "hi\n"
    for bad in (block("C:/a/b.py", "3:6", "x"), block("C:/other.py", "3:5", "x"),
                block("C:/a/b.py", "3:5", "x", extra="1"), block("C:/a/b.py", "3:5", "x")[:-1],
                block("C:/a/b.py", "3:5", "x", replacement_b64="not base64!"),
                block("C:/a/b.py", "3:5", "x", replacement_b64="////"),   # valid b64, invalid UTF-8
                "```" + block("C:/a/b.py", "3:5", "x") + "```"):
        with pytest.raises(mt.PatchError):
            mt.verify_block(bad, spec, "C:/a/b.py", "3:5")


def test_spec_is_accepted_by_mulevalidator_constructor():
    MuleValidator(mt.build_slice_spec("x.py", "1:1"))


# ---- patching ---------------------------------------------------------------
def test_patch_inserts_comment_and_keeps_rest(py):
    r = run(py, 1, 1, answer('# Mule protocol package\n"""doc"""\n'))
    assert r["wrote"] and r["syntax"] == "py_compile"
    assert py.read_text(encoding="utf-8") == '# Mule protocol package\n"""doc"""\nimport os\nx = 1\n'
    assert "+# Mule protocol package" in r["diff"]


def test_syntax_error_leaves_file_untouched(py):
    before = py.read_bytes()
    with pytest.raises(mt.PatchError, match="py_compile failed"):
        run(py, 3, 3, answer("x = = 1\n"))
    assert py.read_bytes() == before
    assert not list(py.parent.glob("*.mule_tmp"))


def test_wrong_ref_echo_is_rejected_and_file_untouched(py):
    before = py.read_bytes()
    with pytest.raises(mt.PatchError, match="MuleValidator"):
        run(py, 3, 3, fake(block(py.resolve().as_posix(), "9:9", "x = 2\n")))
    assert py.read_bytes() == before


def test_retry_recovers_after_a_bad_attempt_with_rising_temperature(py):
    temps = []

    def bad(messages, grammar, **kw):
        temps.append(kw["temperature"])
        return "garbage"

    def good(messages, grammar, **kw):
        temps.append(kw["temperature"])
        return answer("x = 2\n")(messages, grammar, **kw)

    r = run(py, 3, 3, fake(bad, good), retries=2)
    assert r["attempts"] == 2 and temps == [0.0, 0.2]
    assert py.read_text(encoding="utf-8").endswith("x = 2\n")


def test_dry_run_writes_nothing(py):
    before = py.read_bytes()
    r = run(py, 3, 3, answer("x = 2\n"), dry_run=True)
    assert r["wrote"] is False and py.read_bytes() == before


def test_crlf_files_stay_crlf(tmp_path):
    p = tmp_path / "w.py"
    p.write_bytes(b"a = 1\r\nb = 2\r\n")
    run(p, 2, 2, answer("b = 3\n"))
    assert p.read_bytes() == b"a = 1\r\nb = 3\r\n"


def test_json_files_are_checked(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{\n  "a": 1\n}\n', encoding="utf-8")
    run(p, 2, 2, answer('  "a": 2\n'))
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 2}
    with pytest.raises(mt.PatchError, match="json.loads failed"):
        run(p, 2, 2, answer('  "a": \n'))
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 2}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_files_use_node_check(tmp_path):
    p = tmp_path / "s.js"
    p.write_text("const a = 1;\nconst b = 2;\n", encoding="utf-8")
    run(p, 2, 2, answer("const b = 3;\n"))
    with pytest.raises(mt.PatchError, match="node --check failed"):
        run(p, 2, 2, answer("const b = ;\n"))
    assert p.read_text(encoding="utf-8") == "const a = 1;\nconst b = 3;\n"


def test_range_and_size_guards(py):
    for s, e in ((0, 1), (2, 99), (3, 2)):
        with pytest.raises(mt.PatchError, match="outside the file"):
            run(py, s, e, fake("never called"))
    big = py.parent / "big.py"
    big.write_text("x = 1\n" * 200, encoding="utf-8")
    with pytest.raises(mt.PatchError, match="cap is 80"):
        run(big, 1, 81, fake("never called"))
    with pytest.raises(mt.PatchError, match="no such file"):
        run(py.parent / "missing.py", 1, 1, fake("never called"))


def test_main_exit_codes(py, capsys, monkeypatch):
    monkeypatch.setattr(mt.runners, "generate", lambda *a, **k: answer("x = 9\n")(a[2], None))
    assert mt.main([str(py), "3", "3", "set x to 9"]) == 0
    assert "OK: patched" in capsys.readouterr().out
    monkeypatch.setattr(mt.runners, "generate", lambda *a, **k: "nope")
    assert mt.main([str(py), "3", "3", "x", "--retries", "1"]) == 1
    assert "REJECTED BY MULE" in capsys.readouterr().out


def test_prompt_carries_slice_and_its_base64():
    cur = "x = 1\n"
    msgs = mt.build_messages("a/b.py", "3:3", "set x to 2", cur)
    last = msgs[-1]["content"]
    assert "target=a/b.py\nref=3:3\ninstruction=set x to 2" in last
    assert base64.b64encode(cur.encode()).decode() in last
    shot = json.loads(msgs[2]["content"])          # the worked example must itself be valid
    assert base64.b64decode(shot["replacement_b64"]).decode() == "y = 1\nprint(y)\n"
