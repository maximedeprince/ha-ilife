"""Tests that need a running Home Assistant (pytest-homeassistant-custom-component).

The plain `tests/` suite stays free of Home Assistant on purpose. These ones set the
integration up for real, with the cloud replaced, to check what only shows at that
level: the pace of the calls over a simulated day, entity availability, services.

    pip install pytest-homeassistant-custom-component==0.13.373  # Home Assistant 2026.10.2
    python -m pytest tests_ha -o asyncio_mode=auto
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/ilife."""
    return
