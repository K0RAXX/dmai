"""Shared fixtures.  Engine tests must never touch a network or a model."""

from __future__ import annotations

import pytest

from dmai.engine.models import Campaign, CampaignSettings, GameState


@pytest.fixture
def campaign() -> Campaign:
    """A deterministic campaign: fixed seed so dice are reproducible."""
    return Campaign(
        name="The Tomb of Small Errors",
        settings=CampaignSettings(rng_seed=1234),
    )


@pytest.fixture
def state(campaign: Campaign) -> GameState:
    return GameState(campaign=campaign)
