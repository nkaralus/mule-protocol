"""Mule Protocol: deterministic validation of slotted JSON task blocks."""
import json
from importlib import resources

__version__ = "0.2.0"


def load_spec() -> dict:
    """Load the packaged spec (works from a wheel, not just a checkout)."""
    with resources.files("mule").joinpath("spec/mule_spec.json").open("r", encoding="utf-8") as f:
        return json.load(f)


from .validator import MuleValidator  # noqa: E402

__all__ = ["MuleValidator", "load_spec", "__version__"]
