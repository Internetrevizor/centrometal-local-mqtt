"""These tests run the integration inside a real Home Assistant, against the real app on loopback. They need
pytest-homeassistant-custom-component, so `python -m unittest` does not collect them; CI runs them with pytest."""
from pathlib import Path

import custom_components  # at this point, the empty package of the test plugin
import pytest

custom_components.__path__.append(str(Path(__file__).resolve().parents[2] / "custom_components"))


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    yield
