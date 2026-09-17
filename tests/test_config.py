from claude_spend import cli, config, settings


def test_parse_ignores_comments_quotes_and_export():
    text = "# comment\nexport CLAUDE_SPEND_PORT = 4319\nCLAUDE_SPEND_DB='/tmp/x.db'\njunk line\n"
    assert config.parse(text) == {"CLAUDE_SPEND_PORT": "4319", "CLAUDE_SPEND_DB": "/tmp/x.db"}


def test_get_prefers_env_over_file(tmp_path, monkeypatch):
    config.path().parent.mkdir(parents=True, exist_ok=True)
    config.path().write_text("CLAUDE_SPEND_PORT=4319\n")
    assert config.get("CLAUDE_SPEND_PORT") == "4319"
    monkeypatch.setenv("CLAUDE_SPEND_PORT", "5000")
    assert config.get("CLAUDE_SPEND_PORT") == "5000"
    assert config.get("CLAUDE_SPEND_HOST", "127.0.0.1") == "127.0.0.1"


def test_set_values_replaces_in_place_and_keeps_comments():
    p = config.set_values({"CLAUDE_SPEND_PORT": "4319"})
    p.write_text("# my notes\nCLAUDE_SPEND_PORT=4319\nCLAUDE_SPEND_HOST=127.0.0.1\n")
    config.set_values({"CLAUDE_SPEND_PORT": "4320", "CLAUDE_SPEND_DB": "/tmp/a.db"})
    assert p.read_text() == "# my notes\nCLAUDE_SPEND_PORT=4320\nCLAUDE_SPEND_HOST=127.0.0.1\nCLAUDE_SPEND_DB=/tmp/a.db\n"


def test_setup_port_is_saved_to_config(tmp_path):
    s = tmp_path / "settings.json"
    assert cli.main(["setup", "--settings", str(s), "--port", "4321"]) == 0
    assert settings.current_endpoint(s) == "http://127.0.0.1:4321"
    assert config.load()["CLAUDE_SPEND_PORT"] == "4321"


def test_setup_explicit_config_path(tmp_path):
    s, c = tmp_path / "settings.json", tmp_path / "other" / "cfg"
    assert cli.main(["setup", "--settings", str(s), "--port", "4322", "--config", str(c)]) == 0
    assert config.load(c) == {"CLAUDE_SPEND_PORT": "4322"}
    assert not config.path().exists()
