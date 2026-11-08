"""load_spec() must read the spec of the package it belongs to, also when that package is vendored under another name."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "mule"


def test_a_vendored_copy_reads_its_own_spec_not_an_installed_mule(tmp_path):
    vend = tmp_path / "host" / "_vendor"
    shutil.copytree(PKG, vend / "mule", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "host" / "__init__.py").write_text("")
    (vend / "__init__.py").write_text("")
    spec = json.loads((vend / "mule" / "spec" / "mule_spec.json").read_text())
    spec["title"] = "VENDORED"
    (vend / "mule" / "spec" / "mule_spec.json").write_text(json.dumps(spec))
    decoy = tmp_path / "decoy" / "mule"                  # another "mule" on the path, with a different spec
    decoy.mkdir(parents=True)
    (decoy / "__init__.py").write_text("")
    (decoy / "spec").mkdir()
    (decoy / "spec" / "mule_spec.json").write_text('{"title": "DECOY"}')
    code = "from host._vendor.mule import load_spec; print(load_spec()['title'])"
    r = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path[:0] = {[str(tmp_path / 'decoy'), str(tmp_path)]!r}; {code}"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "VENDORED"
