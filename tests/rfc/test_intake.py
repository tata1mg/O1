"""`/rfc` in plain words: the prompt becomes flags; a prompt without a PRD link is discarded."""

import json

import pytest

from rfc_agent import intake

PRD = "https://1mgtech.atlassian.net/wiki/spaces/DD/pages/5705302030/PRD+Share+lab+report+mWeb"
FIG = "https://www.figma.com/design/uwvYvW3cBm2DNKXgfIUXC5?node-id=0-1"
PROMPT = (f"Build an rfc referring this prd {PRD} and ensure we pick the latest vsat flows across repos "
          "instead of older code.")


def test_flags_and_subcommands_are_not_prompts():
    assert not intake.is_prompt("--prd x.md")
    assert not intake.is_prompt("doctor")
    assert not intake.is_prompt("feedback --run-dir /r")
    assert not intake.is_prompt("   ")
    assert intake.is_prompt(PROMPT)


def test_prompt_gives_the_prd_and_the_rest_as_instructions():
    got = intake.parse_prompt(PROMPT)
    assert got["prd"] == PRD
    assert got["figma"] == []
    assert "latest vsat flows" in got["instructions"] and PRD not in got["instructions"]


def test_figma_links_in_the_prompt_become_figma_flags():
    got = intake.parse_prompt(f"rfc for {PRD}, design: {FIG}. Use only the mWeb flow, skip native apps.")
    assert got["figma"] == [FIG]
    assert FIG not in got["instructions"] and "mWeb flow" in got["instructions"]


def test_a_prompt_that_only_says_build_an_rfc_has_no_instructions():
    assert intake.parse_prompt(f"Build an rfc from {PRD}")["instructions"] == ""


def test_a_prompt_without_a_confluence_link_is_discarded():
    assert intake.parse_prompt("Build an rfc for sharing reports, see https://example.com/doc") is None
    assert intake.to_argv("Build an rfc for sharing reports", save_dir=None) is None


def test_to_argv_saves_the_prompt_and_instructions_as_files(tmp_path):
    argv = intake.to_argv(PROMPT, save_dir=tmp_path)
    assert argv[:2] == ["--prd", PRD]
    assert argv[argv.index("--input-mode") + 1] == "prompt"
    instr = argv[argv.index("--instructions") + 1]
    prompt = argv[argv.index("--prompt-text") + 1]
    assert "latest vsat flows" in intake.read_text_arg(instr)
    assert intake.read_text_arg(prompt) == PROMPT


def test_read_text_arg_takes_a_file_or_the_text(tmp_path):
    f = tmp_path / "i.md"
    f.write_text("  from a file \n")
    assert intake.read_text_arg(str(f)) == "from a file"
    assert intake.read_text_arg("just text") == "just text"
    assert intake.read_text_arg(None) == ""


def test_clip_words_keeps_the_first_300_words():
    text = " ".join(f"w{i}" for i in range(500))
    clipped = intake.clip_words(text)
    assert clipped.startswith("w0 w1") and "w299" in clipped and "w300" not in clipped
    assert clipped.endswith("[200 more words]")
    assert intake.clip_words("short text") == "short text"


def test_cli_prints_flags_for_a_prompt_and_passes_flags_through(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(intake, "to_argv", lambda text, save_dir=None: ["--prd", PRD, "--input-mode", "prompt"])
    assert intake.main(["--json", "--", PROMPT]) == 0
    assert json.loads(capsys.readouterr().out)["argv"] == ["--prd", PRD, "--input-mode", "prompt"]
    assert intake.main(["--shell", "--", f"--prd {PRD} --yes"]) == 0
    assert capsys.readouterr().out.strip() == f"--prd {PRD} --yes"


def test_cli_exits_2_and_says_why_when_there_is_no_prd_link(capsys):
    assert intake.main(["--json", "--", "Build an rfc for sharing reports"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "Confluence PRD link" in out.err


def test_cli_flags_pass_through_unchanged(capsys):
    assert intake.main(["--json", "--", "--prd x.md --yes"]) == 0
    assert json.loads(capsys.readouterr().out)["argv"] == ["--prd", "x.md", "--yes"]


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_input_is_not_a_prompt(text):
    assert intake.main(["--json", "--", text]) == 0


def test_a_wrong_rfc_option_names_it_and_the_supported_ones(capsys):
    """#29: `/rfc --prd x --figmaa y` used to end as "stopped" with no reason."""
    import pytest

    from rfc_agent import run

    with pytest.raises(SystemExit) as e:
        run.main(["--prd", "x", "--bogus", "1", "--figmaa=u"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("[rfc] STOP: Invalid parameters found: --bogus, --figmaa (did you mean --figma?), "
                          "please check supported parameters while running the /rfc command")
    assert "Supported:" in err and "--prd" in err and "--instructions" in err


def test_every_command_line_entry_point_reports_a_wrong_option_the_same_way(capsys):
    """Not a check on any one option: the stage CLIs, the harness (test-agent) and its helpers all use it."""
    import importlib

    import pytest

    from harness import cli as harness_cli

    cases = [("rfc_agent.scope.cli", "rfc"), ("rfc_agent.investigate.cli", "rfc"), ("rfc_agent.draft.cli", "rfc"),
             ("rfc_agent.review.cli", "rfc"), ("rfc_agent.revise.cli", "rfc"), ("rfc_agent.summarize.cli", "rfc")]
    for mod, tag in cases:
        with pytest.raises(SystemExit) as e:
            importlib.import_module(mod).main(["--definitely-not-an-option", "1"])
        assert e.value.code == 2
        assert capsys.readouterr().err.startswith(f"[{tag}] STOP: Invalid parameter found: --definitely-not-an-option")
    # the harness keeps its own exit code (2 means a gate failure there) and names the sub-command's options
    with pytest.raises(SystemExit) as e:
        harness_cli.main(["run", "testing", "--feature", "x", "--definately-wrong"])
    err = capsys.readouterr().err
    assert e.value.code == 1
    assert err.startswith("[harness] STOP: Invalid parameter found: --definately-wrong")
    assert "harness run testing" in err and "--feature" in err
