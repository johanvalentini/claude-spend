import json

from claude_spend import cli, settings


def test_enable_creates_file_and_preserves_other_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "opus", "env": {"FOO": "1"}}))
    settings.enable(4319, path)
    data = json.loads(path.read_text())
    assert data["model"] == "opus"
    assert data["env"]["FOO"] == "1"
    assert data["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4319"
    assert data["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"


def test_enable_creates_missing_file(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    settings.enable(4318, path)
    assert settings.current_endpoint(path) == "http://127.0.0.1:4318"


def test_disable_removes_only_telemetry_keys(tmp_path):
    path = tmp_path / "settings.json"
    settings.enable(4318, path)
    data = json.loads(path.read_text())
    data["env"]["KEEP"] = "yes"
    path.write_text(json.dumps(data))
    removed = settings.disable(path)
    assert len(removed) == len(settings.telemetry_env(4318))
    assert json.loads(path.read_text()) == {"env": {"KEEP": "yes"}}


def test_disable_drops_empty_env(tmp_path):
    path = tmp_path / "settings.json"
    settings.enable(4318, path)
    settings.disable(path)
    assert json.loads(path.read_text()) == {}


def test_cli_setup_refuses_foreign_endpoint_without_force(tmp_path, capsys):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://elsewhere:4317"}}))
    assert cli.main(["setup", "--settings", str(path)]) == 1
    assert "already exports" in capsys.readouterr().err
    assert settings.current_endpoint(path) == "http://elsewhere:4317"
    assert cli.main(["setup", "--settings", str(path), "--force"]) == 0
    assert settings.current_endpoint(path) == "http://127.0.0.1:4318"


def test_cli_setup_purge(tmp_path):
    path = tmp_path / "settings.json"
    settings.enable(4318, path)
    assert cli.main(["setup", "--purge", "--settings", str(path)]) == 0
    assert settings.current_endpoint(path) is None


def test_cli_help_and_unknown(capsys):
    assert cli.main([]) == 0
    assert "claude-spend setup" in capsys.readouterr().out
    assert cli.main(["bogus"]) == 2
    assert cli.main(["--version"]) == 0
