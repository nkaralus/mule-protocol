"""Keep bridge tests away from the real DLQ, mode file and node URLs."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_bridge_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MULE_DLQ", str(tmp_path / "dlq.jsonl"))
    monkeypatch.setenv("MULE_MODE_FILE", str(tmp_path / "mode.json"))   # absent unless a test writes it
    monkeypatch.delenv("MULE_URL", raising=False)
    monkeypatch.delenv("MULE_EYES_URL", raising=False)
