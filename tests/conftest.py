"""Keep every test off the network, including the harmonisation check's overview read."""

from __future__ import annotations

import pytest

from dosojos_sat import stac


@pytest.fixture(autouse=True)
def no_overview_reads(monkeypatch):
    """Scenes flagged false are raw unless a test says what their pixels show."""
    monkeypatch.setattr(stac, "_dark_red_dn", lambda scene: None)
    stac._harmonised_cache.clear()
    yield
    stac._harmonised_cache.clear()
