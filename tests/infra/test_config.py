"""The config loader: config.harness.json (shipped) < config.json (yours) < env vars."""

import json

import pytest

from rfc_agent.config import HARNESS, KNOWN_SECTIONS, Config

HARNESS_TEST = {
    "AGENT": {"CLAUDE_BIN": "claude", "RUN_TIMEOUT": 2400},
    "MCP": {"mcpServers": {}},
    "LANGFUSE": {"HOST": "https://cloud.langfuse.com", "PUBLIC_KEY": "", "SECRET_KEY": ""},
}


def _write(tmp_path, user: dict | None):
    h = tmp_path / "config.harness.json"
    h.write_text(json.dumps(HARNESS_TEST))
    loc = tmp_path / "config.json"
    if user is not None:
        loc.write_text(json.dumps(user))
    return loc, h


def test_missing_config_json_fails_loudly_and_says_how_to_fix(tmp_path):
    loc, h = _write(tmp_path, None)
    with pytest.raises(RuntimeError, match="not found.*rfc-agent:setup"):
        Config(local=loc, harness=h)


def test_a_small_user_file_gets_every_default(tmp_path):
    loc, h = _write(tmp_path, {"REPOS_ROOT": "~/x"})
    cfg = Config(local=loc, harness=h)
    assert cfg.get("REPOS_ROOT") == "~/x"
    assert cfg.get("AGENT.RUN_TIMEOUT") == 2400, "a key the user never wrote comes from the defaults"


def test_user_values_win_and_dicts_merge_key_by_key(tmp_path):
    user = {"ATLASSIAN": {"SITE": "https://team.atlassian.net"},
            "LANGFUSE": {"HOST": "https://langfuse.team", "SECRET_KEY": "sk"},
            "MCP": {"mcpServers": {"fleetlens": {"url": "http://fl/mcp", "headers": {"Authorization": "t"}}}},
            "AGENT": {"RUN_TIMEOUT": 99}}
    loc, h = _write(tmp_path, user)
    cfg = Config(local=loc, harness=h)
    assert cfg.get("ATLASSIAN.SITE") == "https://team.atlassian.net"
    assert cfg.get("LANGFUSE.HOST") == "https://langfuse.team" and cfg.get("LANGFUSE.SECRET_KEY") == "sk"
    assert cfg.get("LANGFUSE.PUBLIC_KEY") == "", "a key you leave out keeps the harness value"
    assert cfg.get("AGENT.RUN_TIMEOUT") == 99 and cfg.get("AGENT.CLAUDE_BIN") == "claude"
    assert cfg.get("MCP.mcpServers.fleetlens.headers.Authorization") == "t"


def test_env_override_wins_and_coerces(tmp_path, monkeypatch):
    loc, h = _write(tmp_path, {"REPOS_ROOT": "~/x"})
    monkeypatch.setenv("RFC_AGENT_REPOS_ROOT", "/elsewhere")
    cfg = Config(local=loc, harness=h)
    assert cfg.get("REPOS_ROOT") == "/elsewhere"
    assert cfg.get("AGENT.RUN_TIMEOUT") == 2400  # untouched, still typed int


def test_unknown_sections_are_reported_not_fatal(tmp_path, capsys):
    loc, h = _write(tmp_path, {"REPO_ROOT": "~/typo", "PIPELINE": ["ingest"], "_note": "docs are fine"})
    cfg = Config(local=loc, harness=h)
    err = capsys.readouterr().err
    assert "REPO_ROOT" in err and "PIPELINE" in err and "_note" not in err
    assert cfg.get("AGENT.RUN_TIMEOUT") == 2400


def test_bad_json_names_the_file(tmp_path):
    loc, h = _write(tmp_path, None)
    loc.write_text("{not json")
    with pytest.raises(RuntimeError, match="config.json is not valid JSON"):
        Config(local=loc, harness=h)


def test_harness_file_holds_no_org_values_and_only_known_sections():
    data = json.loads(HARNESS.read_text())
    text = HARNESS.read_text().lower()
    assert "atlassian.net" not in text, "team values are the user's, not the plugin's"
    user_supplied = {"ATLASSIAN", "REPOS_ROOT", "RUNS_ROOT", "SCOPE", "SERVICES_MAP", "REPOS_EXCLUDE", "MCP", "OBS"}
    assert not (user_supplied & set(data)), "user-supplied sections belong in the user's config.json"
    assert not ({"CLAUDE_BIN", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN_FILE", "EXTRA_DISALLOWED_TOOLS"}
                & set(data.get("AGENT", {}))), "the user's machine, login and connectors"
    assert not ({"HOST", "PUBLIC_KEY", "SECRET_KEY", "ENVIRONMENT"} & set(data.get("LANGFUSE", {})))
    assert not [k for k in data if not k.startswith("_") and k not in KNOWN_SECTIONS]


def test_materialize_mcp_subset(tmp_path):
    user = {"MCP": {"mcpServers": {
        "atlassian": {"command": "uvx", "args": ["mcp-atlassian"]},
        "bitbucket": {"command": "node", "args": ["~/tools/bb.js"]},
    }}}
    loc, h = _write(tmp_path, user)
    cfg = Config(local=loc, harness=h)

    dest = tmp_path / "sub.json"
    cfg.materialize_mcp(("bitbucket",), dest)
    written = json.loads(dest.read_text())
    assert list(written["mcpServers"]) == ["bitbucket"]
    assert written["mcpServers"]["bitbucket"]["args"][0].endswith("/tools/bb.js")  # ~ expanded
    assert "~" not in written["mcpServers"]["bitbucket"]["args"][0]

    cfg.materialize_mcp((), dest)  # empty subset => guaranteed no-MCP file
    assert json.loads(dest.read_text()) == {"mcpServers": {}}

    with pytest.raises(RuntimeError, match="figma"):
        cfg.materialize_mcp(("figma",), dest)
