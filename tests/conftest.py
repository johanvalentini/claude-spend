import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Never read or write the developer's real ~/.config/claude-spend/config."""
    monkeypatch.setenv("CLAUDE_SPEND_CONFIG", str(tmp_path / "config"))
    for k in ("CLAUDE_SPEND_PORT", "CLAUDE_SPEND_HOST", "CLAUDE_SPEND_DB", "CLAUDE_SPEND_LOG"):
        monkeypatch.delenv(k, raising=False)
