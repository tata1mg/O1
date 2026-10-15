"""Fixes from the independent review of `harness/agnostic-kernel` (research/REVIEW.md).

Each test names the finding it pins. They are grouped here rather than scattered because a reviewer
coming back to the branch should be able to see, in one file, that every finding has a test that would
have caught it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness import cli as harness_cli
from harness import flags as flagmod
from harness.contracts import RunContext, Stage
from harness.events import Event as Ev
from harness.events import EventType, EventWriter, ToolKind, summarize

ROOT = Path(__file__).resolve().parents[2]

# The registry is populated by IMPORTS (each workflow package registers its flags, each engine module
# its construction flags), so a test that reasons about the registry must load it the same way the CLI
# does. Doing it at module scope keeps every test below independent of import order.
harness_cli.load_registries()


# ---- BLOCKER 1.1 — credentials must never reach a run dir, a log or stdout -------------------------

SECRET = "hunter2SECRET"


def test_a_secret_flags_value_is_never_serialised():
    fl = flagmod.resolve(env={"TCMS_PASSWORD": SECRET})
    assert fl.get("testing.kiwi_password") == SECRET, "the code that needs the password still gets it"
    assert SECRET not in fl.dump(), "flags.json must not carry it"
    assert SECRET not in str(fl.as_dict()), "as_dict feeds observability"
    dumped = json.loads(fl.dump())["testing.kiwi_password"]
    assert dumped["value"] == flagmod.REDACTED
    assert dumped["layers"]["env"] == flagmod.REDACTED, \
        "a credential leaking only through layers.env leaks just as completely"
    assert dumped["secret"] is True, "the FACT of being set stays observable"


def test_the_kiwi_credentials_are_marked_secret():
    reg = flagmod.registry()
    assert reg["testing.kiwi_password"].secret is True
    # kiwi_user is DELIBERATELY not secret (src/workflows/testing/flags.py:28): Kiwi writes it into every
    # TestRun's manager field where all accounts read it, and "which account did this run use" is the first
    # debugging question — redacting it costs debuggability for no confidentiality gain.
    assert reg["testing.kiwi_user"].secret is False


def test_credential_shaped_flags_are_all_marked_secret():
    """A tripwire for flags added later: an env var named like a credential must be marked secret.
    Cheaper than re-reviewing every new flag, and it fails at the moment the flag is added."""
    import re

    shape = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|_KEY$|APIKEY|CREDENTIAL)")
    offenders = [f.name for f in flagmod.registry().values()
                 if shape.search(f.env_var) and not f.secret]
    assert not offenders, (
        f"credential-shaped flags not marked secret={offenders} — add `secret=True` so the value never "
        f"reaches flags.json, `flags --json` or a CI log."
    )


def test_flags_json_is_scoped_to_the_flags_that_could_affect_this_run(tmp_path, monkeypatch):
    """Workflow discovery imports EVERY package under workflows/, so an unscoped dump put another
    workflow's flags — credentials included — into every run dir, `rfc-agent` runs included."""
    from rfc_agent.runner import run_pipeline

    monkeypatch.setenv("TCMS_PASSWORD", SECRET)
    (tmp_path / "inputs.json").write_text("{}")

    def engine(stage, ctx):
        ctx.path("out.md").write_text("x")
        from harness.contracts import StageResult

        return StageResult(engine_desc="fake")

    stage = Stage(name="s", requires=("inputs.json",), outputs=("out.md",),
                  build_prompt=lambda c: "", gate=lambda c: [], engine=engine)
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=None)
    run_pipeline([stage], ctx, flags=flagmod.resolve(env={"TCMS_PASSWORD": SECRET}), workflow="testing")

    text = (tmp_path / "flags.json").read_text()
    assert SECRET not in text
    names = set(json.loads(text))
    assert "engine" in names and "testing.storage" in names
    assert any(n.startswith("pi.") for n in names), "engine flags DID affect this run's resolution"


def test_an_unscoped_dump_is_still_available_for_the_full_registry():
    """`dump(only=None)` keeps working — it is what `harness flags --json` and debugging want."""
    assert "testing.storage" in json.loads(flagmod.resolve(env={}).dump())


def test_describe_and_explain_redact_a_secret_default():
    rows = {r["name"]: r for r in flagmod.describe()}
    assert rows["testing.kiwi_password"]["secret"] is True
    res = subprocess.run([sys.executable, "-m", "harness.cli", "flags", "--explain",
                          "testing.kiwi_password", "--json"], capture_output=True, text=True,
                         cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin",
                                        "HOME": str(Path.home()), "TCMS_PASSWORD": SECRET})
    assert res.returncode == 0 and SECRET not in res.stdout
    assert json.loads(res.stdout)["value"] == flagmod.REDACTED


# ---- BLOCKER 1.2 — engine construction flags must reach the adapter -------------------------------

def test_engine_from_flags_forwards_flags_to_an_adapter_that_takes_them():
    from harness.engines import engine_from_flags

    eng = engine_from_flags("pi", flagmod.resolve(cli_sets={"pi.provider": "anthropic"}, env={}))
    assert eng._flag("pi.provider") == "anthropic", \
        "--set pi.provider was silently ignored: every pi.* construction flag was inert"


def test_engine_from_flags_still_builds_an_adapter_that_does_not_take_flags():
    """Adapters differ on purpose; passing `flags` unconditionally would TypeError on this one."""
    from harness.engines import engine_from_flags
    from harness.engines.claude_code import ClaudeCodeEngine

    assert isinstance(engine_from_flags("claude-code", flagmod.resolve(env={})), ClaudeCodeEngine)


def test_get_engine_drops_flags_only_for_constructors_that_reject_it():
    from harness.engines import get_engine, register

    seen: dict = {}

    class Taker:
        name = "taker"

        def __init__(self, flags=None):
            seen["flags"] = flags

    class Refuser:
        name = "refuser"

        def __init__(self):
            seen["built"] = True

    register("taker", Taker)
    register("refuser", Refuser)
    sentinel = flagmod.resolve(env={})
    get_engine("taker", flags=sentinel)
    assert seen["flags"] is sentinel
    get_engine("refuser", flags=sentinel)
    assert seen["built"] is True


def test_engine_flags_are_registered_for_the_cli_to_list_and_validate():
    """The CLI half of the same bug: `pi.*` flags register on import, and nothing imported the engine
    modules, so `harness flags` omitted them and `--set pi.provider=x` was rejected as unknown."""
    res = subprocess.run([sys.executable, "-m", "harness.cli", "flags", "--json"],
                         capture_output=True, text=True, cwd=ROOT,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin",
                              "HOME": str(Path.home())})
    names = {r["name"] for r in json.loads(res.stdout)}
    assert any(n.startswith("pi.") for n in names), "engine flags must be listed"
    assert "replay.recording" in names


# ---- BLOCKER 3.1 / 3.2 — summarize must match streams.py -----------------------------------------

def _denial_and_two_results(path: Path) -> None:
    """One refusal firing BOTH signals for the same call, then a post-result bonus turn."""
    with EventWriter(path) as w:
        w.emit(Ev(t=EventType.INIT, model="m", session_id="s1"))
        w.emit(Ev(t=EventType.TOOL_START, tool="Bash", kind=ToolKind.SHELL, call_id="tu1",
                  input={"command": "rm -rf /"}))
        w.emit(Ev(t=EventType.DENIAL, call_id="tu1", tool="Bash", reason="permission_denied"))
        w.emit(Ev(t=EventType.DENIAL, call_id="tu1", tool="Bash",
                  reason="DENIED by rfc_agent write-guard (guard_writes)"))
        w.emit(Ev(t=EventType.RESULT, ok=True, text="first", cost_usd=1.0, num_turns=3,
                  usage={"input_tokens": 1}, session_id="s1"))
        w.emit(Ev(t=EventType.RESULT, ok=False, text="bonus", cost_usd=2.5, num_turns=5,
                  usage={"input_tokens": 9}, session_id="s1", error="api_error"))


def test_summarize_counts_a_denial_once_per_call_id(tmp_path):
    """One refusal fires two signals on CLI >= 2.1.224; counting events double-counts every denial,
    and `denials` is charted as a bad-run predictor."""
    p = tmp_path / "t.events.jsonl"
    _denial_and_two_results(p)
    assert summarize(p).denials == 1


def test_summarize_matches_parse_stream_on_the_same_run(tmp_path):
    """Side by side with the reference implementation, on the stream shape the review used."""
    from rfc_agent.streams import parse_stream

    raw = "\n".join(json.dumps(x) for x in [
        {"type": "system", "subtype": "init", "model": "m", "session_id": "s1"},
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "rm -rf /"}}]}},
        {"type": "system", "subtype": "permission_denied", "tool_use_id": "tu1", "tool_name": "Bash"},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1", "is_error": True,
             "content": "DENIED by rfc_agent write-guard (guard_writes)"}]}},
        {"type": "result", "is_error": False, "result": "first", "total_cost_usd": 1.0,
         "num_turns": 3, "usage": {"input_tokens": 1}},
        {"type": "result", "is_error": False, "result": "bonus", "total_cost_usd": 2.5,
         "num_turns": 5, "usage": {"input_tokens": 9}},
    ])
    from harness.engines.claude_translate import translate_file

    stream = tmp_path / "s.stream.jsonl"
    stream.write_text(raw)
    trace = translate_file(stream, tmp_path / "t.events.jsonl")

    ref, got = parse_stream(raw), summarize(trace)
    assert got.denials == ref.denials == 1
    assert got.cost_usd == ref.cost_usd == 2.5, "cost is LAST-wins: a bonus turn's spend still happened"
    assert got.num_turns == ref.num_turns == 5
    assert got.usage == ref.usage == {"input_tokens": 9}
    assert got.final_text == ref.final_text == "first", "the VERDICT stays first-result-wins"
    assert got.is_error is ref.is_error is False
    assert got.extra_results == ref.extra_results == 1


def test_summarize_carries_the_bonus_turn_counters(tmp_path):
    """So a non-Claude engine can record the same `post_result_turn` decision the Claude path does."""
    p = tmp_path / "t.events.jsonl"
    _denial_and_two_results(p)
    s = summarize(p)
    assert s.extra_results == 1 and s.extra_result_error is True
    assert s.final_text == "first" and s.is_error is False


def test_summarize_gives_anonymous_denials_distinct_identities(tmp_path):
    """An engine that reports no call_id still gets an honest count."""
    p = tmp_path / "t.events.jsonl"
    with EventWriter(p) as w:
        w.emit(Ev(t=EventType.DENIAL, reason="one"))
        w.emit(Ev(t=EventType.DENIAL, reason="two"))
    assert summarize(p).denials == 2


def test_summarize_keeps_a_none_cost_none(tmp_path):
    """A7: a result with no cost must not become 0.0."""
    p = tmp_path / "t.events.jsonl"
    with EventWriter(p) as w:
        w.emit(Ev(t=EventType.RESULT, ok=True, text="done"))
    assert summarize(p).cost_usd is None


# ---- SHOULD-FIX 1.4 — a translation failure must never lose a successful stage --------------------

def test_run_stage_survives_a_translation_failure(tmp_path, monkeypatch, capsys):
    """`run_stage`'s loop runs AFTER a successful stage and outside the runner's except EngineFailure,
    so an exception there skipped the ledger, the gate, the manifest and the crash accounting and
    exited with a traceback — losing the whole stage's spend to a failed write."""
    from harness.contracts import StageResult, rotated_stream_path, stream_path
    from harness.engines.claude_code import ClaudeCodeEngine
    from rfc_agent import engine as rfc_engine

    def fake_claude_engine(stage, ctx):
        stream_path(tmp_path, "gen").parent.mkdir(parents=True, exist_ok=True)
        stream_path(tmp_path, "gen").write_text('{"type":"result","is_error":false,"result":"ok"}')
        rotated_stream_path(tmp_path, "gen", 1).write_text('{"type":"result","is_error":false}')
        return StageResult(final_text="ok", engine_desc="claude:m", cost_usd=3.0)

    monkeypatch.setattr(rfc_engine, "claude_engine", fake_claude_engine)
    monkeypatch.setattr("harness.engines.claude_translate.translate_file",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    stage = type("S", (), {"name": "gen"})()
    ctx = type("C", (), {"run_dir": tmp_path})()
    res = ClaudeCodeEngine().run_stage(stage, ctx)
    assert res.cost_usd == 3.0, "the successful stage's result must survive"
    assert "canonical trace not written" in capsys.readouterr().err


def test_write_trace_never_raises(tmp_path, monkeypatch):
    from harness.engines.claude_code import ClaudeCodeEngine

    monkeypatch.setattr("harness.engines.claude_translate.translate_file",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ClaudeCodeEngine.write_trace(tmp_path / "s.jsonl",
                                        tmp_path / "logs" / "x.events.jsonl") is None


# ---- SHOULD-FIX 1.5 / 6.1 — the bridge works end to end -------------------------------------------

def test_ctx_flag_honours_the_legacy_config_key_without_resolved_flags(tmp_path):
    """The embedder path: `run_pipeline` called directly, no flags resolved. Before this, a config.json
    carrying AGENT.HOOK_CANARY:false was silently ignored because the lookup used the FLAG name."""
    class Cfg:
        def get(self, key, default=None):
            return {"AGENT.HOOK_CANARY": False, "SOME.KEY": 6}.get(key, default)

    ctx = RunContext(run_dir=tmp_path, inputs={}, config=Cfg())
    assert ctx.flag("hook_canary", True) is False
    assert ctx.flag("SOME.KEY") == 6, "an unmigrated dotted read still works"
    assert ctx.flag("nonexistent", "fallback") == "fallback"


def test_bridged_flags_are_marked_so_the_listing_does_not_overclaim():
    """The promise is a list an agent can ACT on. A flag nothing reads by name must say so."""
    rows = {r["name"]: r for r in flagmod.describe()}
    assert rows["hook_canary"]["wired"] is True
    assert rows["obs_required"]["wired"] is True


def test_every_bridged_core_flag_really_is_read_through_ctx_flag():
    """The marker must not drift: a flag claiming `wired` needs a `ctx.flag("<name>")` reader."""
    import re

    src = "\n".join((ROOT / "src" / p).read_text()
                    for p in ("rfc_agent/engine.py", "rfc_agent/runner.py"))
    cli_src = (ROOT / "src" / "harness" / "cli.py").read_text()
    for name in ("hook_canary", "obs_required"):
        pattern = re.compile(rf'(ctx\.flag|flags\.get)\(\s*["\']{re.escape(name)}["\']')
        assert pattern.search(src) or pattern.search(cli_src), \
            f"{name} is marked wired but nothing reads it by that name"


def test_the_core_flags_keep_their_legacy_config_keys():
    """An existing config.json carrying AGENT.HOOK_CANARY / LANGFUSE.REQUIRED keeps working."""
    reg = flagmod.registry()
    assert reg["hook_canary"].legacy_config_key == "AGENT.HOOK_CANARY"
    assert reg["obs_required"].legacy_config_key == "LANGFUSE.REQUIRED"




# ---- SHOULD-FIX 6.3 — a typo must not disarm a guard ----------------------------------------------

@pytest.mark.parametrize("word", ["true", "1", "yes", "on", "TRUE", " On "])
def test_boolean_true_words(word):
    assert flagmod.coerce(flagmod.registry()["hooks"], word) is True


@pytest.mark.parametrize("word", ["false", "0", "no", "off", "FALSE", " Off "])
def test_boolean_false_words(word):
    assert flagmod.coerce(flagmod.registry()["hooks"], word) is False


@pytest.mark.parametrize("word", ["fasle", "disabled", "", "maybe", "2x"])
def test_a_bad_boolean_raises_instead_of_silently_disabling_containment(word):
    """`hooks`'s own help calls False 'removes containment — for benchmarks only'."""
    with pytest.raises(ValueError) as exc:
        flagmod.coerce(flagmod.registry()["hooks"], word)
    assert "hooks" in str(exc.value) and "--disable hooks" in str(exc.value)


def test_enable_disable_still_bypass_the_word_list():
    sets = flagmod.parse_cli_sets(None, ["hooks"], ["green_skip"])
    assert flagmod.coerce(flagmod.registry()["hooks"], sets["hooks"]) is True
    assert flagmod.coerce(flagmod.registry()["green_skip"], sets["green_skip"]) is False


def test_a_bad_boolean_from_the_cli_is_a_readable_exit_1():
    res = subprocess.run([sys.executable, "-m", "harness.cli", "run", "testing", "--set", "hooks=fasle",
                          "--feature", "x", "--no-interactive"], capture_output=True, text=True,
                         cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin",
                                        "HOME": str(Path.home())})
    assert res.returncode == 1
    assert "is not a boolean" in res.stderr and "--disable hooks" in res.stderr


# ---- SHOULD-FIX 6.2 — --explain must show the config layer ---------------------------------------

def _explain(name: str, env: dict | None = None, workflow: str | None = None) -> dict:
    args = [sys.executable, "-m", "harness.cli", "flags", "--explain", name, "--json"]
    if workflow:
        args += ["--workflow", workflow]
    res = subprocess.run(args, capture_output=True, text=True, cwd=ROOT,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin",
                              "HOME": str(Path.home()), **(env or {})})
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_explain_names_the_env_layer_as_the_winner():
    out = _explain("testing.storage", env={"HARNESS_TESTING_STORAGE": "kiwi"}, workflow="testing")
    assert out["value"] == "kiwi" and out["winning_layer"] == "env"
    assert out["layers"]["default"] == "json" and out["layers"]["env"] == "kiwi"


def test_explain_exposes_the_bridge_and_the_wired_marker():
    out = _explain("hook_canary")
    assert out["legacy_config_key"] == "AGENT.HOOK_CANARY" and out["wired"] is True


def test_explain_works_without_a_config(monkeypatch):
    """`harness flags` must stay usable on a machine with no config.json — where it is run first."""
    out = _explain("engine")
    assert out["value"] == "claude-code" and out["winning_layer"] == "default"


# ---- SHOULD-FIX 1.3 — the frozen alias keeps exit 2 ----------------------------------------------

def _cli(module_main: str, argv: list[str]) -> int:
    return subprocess.run(
        [sys.executable, "-c", f"import sys; from {module_main} import main; main(sys.argv[1:])", *argv],
        capture_output=True, text=True, cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    ).returncode


def test_harness_uses_exit_1_for_the_same_usage_error():
    res = subprocess.run([sys.executable, "-m", "harness.cli", "run", "testing", "--stages", "evaluate",
                          "--feature", "x"], capture_output=True, text=True, cwd=ROOT,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin",
                              "HOME": str(Path.home())})
    assert res.returncode == 1, "harness has no such history and uses the correct code"
    assert "--run-dir" in res.stderr


# ---- NIT 6.4 — a bad config value is an exit 1, not a traceback -----------------------------------

