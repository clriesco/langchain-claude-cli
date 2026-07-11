"""Shared fixtures: the cassette record/replay harness (v0.3 D1)."""

from __future__ import annotations

import claude_agent_sdk
import pytest

from tests._cassettes import CASSETTE_DIR, RECORDING, CassettePlayer


@pytest.fixture()
def cassette(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Record/replay every claude_agent_sdk.query call made by this test."""
    player = CassettePlayer(CASSETTE_DIR / f"{request.node.name}.json")
    monkeypatch.setattr(
        claude_agent_sdk,
        "query",
        player.recording_query if RECORDING else player.replay_query,
    )
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", player.make_client)
    yield player
    player.save()
    if not RECORDING:
        assert player.index == len(player.exchanges), (
            f"Cassette {player.path.name}: {len(player.exchanges)} exchanges "
            f"recorded but only {player.index} consumed"
        )
