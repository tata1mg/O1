"""M2: scan ledger + trace-backed gates — the fabricated-citation catch."""

import json
from pathlib import Path

from rfc_agent.gates import check_citations_witnessed
from rfc_agent.ledger import build_ledger, write_ledger

FAKE_REPO = Path(__file__).parents[1] / "fixtures" / "fake_repo"  # contains labs_agg/app/handlers.py


def _stream(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


def _tool(name, _id=None, **inp):
    block = {"type": "tool_use", "name": name, "input": inp}
    if _id:
        block["id"] = _id
    return {"type": "assistant", "message": {"content": [block]}}


def _result(_id, content):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": _id, "content": content}
    ]}}


def test_build_ledger_collects_reads_and_searches():
    s = _stream(
        _tool("Read", file_path="/x/labs_agg/app/handlers.py"),
        _tool("Grep", pattern="assured", path="/x/labs_agg"),
        _tool("Bash", command="grep -rn nudge /x/svc_a/app | head"),
        _tool("Write", file_path="findings/findings.md"),
    )
    led = build_ledger(s)
    assert led.files_read == ["/x/labs_agg/app/handlers.py"]
    assert len(led.searches) == 2  # Grep + Bash-grep
    assert led.tool_counts == {"Read": 1, "Grep": 1, "Bash": 1, "Write": 1}


def test_witnessed_via_tool_output_too():
    # the path only appears in a tool RESULT (e.g. grep -n output), not any input — still witnessed
    s = _stream(
        _tool("Grep", _id="t1", pattern="assured"),
        _result("t1", "labs_agg/app/handlers.py:3: assured_lab = True"),
    )
    assert build_ledger(s).witnessed("labs_agg/app/handlers.py")


def test_agent_subagent_report_does_not_witness():
    """An Agent/Task tool_result is the subagent's final report — model PROSE that can name any path:line
    it likes. Letting it witness would reopen the prose-as-evidence bypass this gate exists to close; the
    subagent's own Read/Grep events appear in the stream and witness legitimately on their own."""
    for tool in ("Agent", "Task"):
        s = _stream(
            _tool(tool, _id="t1", prompt="scan labs repo"),
            _result("t1", "I found the handler at labs_agg/app/handlers.py:3 — it sets assured_lab."),
        )
        assert not build_ledger(s).witnessed("labs_agg/app/handlers.py"), tool


def test_result_without_known_origin_does_not_witness():
    """Results witness only when correlated to a clean originating call — an orphan tool_result (no
    matching tool_use id) is excluded, fail-safe. Real SDK streams always carry ids."""
    s = _stream(_result("never-issued", "labs_agg/app/handlers.py:3: hit"))
    assert not build_ledger(s).witnessed("labs_agg/app/handlers.py")


def test_reading_own_findings_does_not_witness():
    """Revision mode REQUIRES reading the previous findings.md, whose content is full of repo citations —
    reading your own writing must not witness them, or the gate goes vacuous on every revision."""
    s = _stream(
        _tool("Read", _id="r1", file_path="/run/dir/findings/findings.md"),
        _result("r1", "- grounded at svc_d/app/models/order.rb:42 [REQ-6]"),
    )
    led = build_ledger(s)
    assert not led.witnessed("svc_d/app/models/order.rb")
    assert led.files_read == ["/run/dir/findings/findings.md"]  # still counted as a read, just not witness


def test_bash_grep_of_own_artifacts_does_not_witness():
    """A Bash command targeting the run's own artifacts is excluded end-to-end: the command string the
    agent authored may embed citation tokens, and the result echoes the artifact's citations back."""
    s = _stream(
        _tool("Bash", _id="b1", command="grep -n 'order.rb' findings/findings.md"),
        _result("b1", "12:- svc_d/app/models/order.rb:42 enum status"),
    )
    assert not build_ledger(s).witnessed("svc_d/app/models/order.rb")


def test_repo_interactions_still_witness_alongside_artifact_reads():
    """The exclusion is per-interaction, not per-stream: repo reads in the same session keep witnessing."""
    s = _stream(
        _tool("Read", _id="r1", file_path="/run/dir/findings/findings.md"),
        _result("r1", "- cites svc_d/app/models/order.rb:42"),
        _tool("Bash", _id="b1", command="grep -n 'enum status' svc_d/app/models/slot_booking.rb"),
        _result("b1", "117:  enum status: {reserved: 0}"),
    )
    led = build_ledger(s)
    assert led.witnessed("svc_d/app/models/slot_booking.rb")  # witnessed via the clean Bash INPUT
    assert not led.witnessed("svc_d/app/models/order.rb")


def test_build_ledger_from_streams_spans_attempts(tmp_path):
    """Witness must span rotated attempt streams: a citation read in attempt 1 and carried over by a
    revision in attempt 2 is genuinely witnessed by this run."""
    from rfc_agent.ledger import build_ledger_from_streams

    a1 = tmp_path / "ground.stream.1.jsonl"
    a1.write_text(_stream(_tool("Read", _id="x", file_path="svc_d/app/models/order.rb")))
    a2 = tmp_path / "ground.stream.jsonl"
    a2.write_text(_stream(_tool("Read", _id="y", file_path="svc_a/app/managers/order.py")))
    led = build_ledger_from_streams([a1, a2])
    assert led.witnessed("svc_d/app/models/order.rb") and led.witnessed("svc_a/app/managers/order.py")


def test_broad_listing_does_not_witness():
    """One `find`/`ls` over the repos used to witness every listed path — 'witnessed' must mean read or
    search-hit, not 'listed once'."""
    s = _stream(
        _tool("Bash", command="find /repos -name '*.py'"),
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "/repos/labs_agg/app/handlers.py\n/repos/svc_a/app/models.py"},
        ]}},
    )
    led = build_ledger(s)
    assert not led.witnessed("labs_agg/app/handlers.py")
    assert not led.witnessed("svc_a/app/models.py")


def test_own_write_does_not_witness():
    """The agent's Write of findings.md must not witness the citations it contains (self-witnessing)."""
    s = _stream(_tool("Write", file_path="findings/findings.md",
                      content="grounded at labs_agg/app/handlers.py:3"))
    assert not build_ledger(s).witnessed("labs_agg/app/handlers.py")


def test_witnessed_gate_catches_fabricated_citation():
    # citation resolves on disk (file exists, line in range) but the agent NEVER touched it
    led = build_ledger(_stream(_tool("Read", file_path="somewhere/else.py")))
    text = "we extend labs_agg/app/handlers.py:3 here"
    failures = check_citations_witnessed(text, FAKE_REPO, led, "findings.md")
    assert failures and "NOT witnessed" in failures[0]


def test_witnessed_gate_passes_traced_citation():
    led = build_ledger(_stream(_tool("Read", file_path=str(FAKE_REPO / "labs_agg/app/handlers.py"))))
    failures = check_citations_witnessed("see labs_agg/app/handlers.py:3", FAKE_REPO, led, "findings.md")
    assert failures == []


def test_witnessed_gate_skips_non_local_repos():
    led = build_ledger(_stream())
    failures = check_citations_witnessed("see ios_app/Sources/Main.swift:9", FAKE_REPO, led, "f")
    assert failures == []  # non-local handled by the resolve check's warning path






def test_write_ledger_renders_json_over_all_streams(tmp_path):
    """write_ledger takes ALL the stage's streams (parallel shards + rotations), not just the main one."""
    sp = tmp_path / "ground.stream.jsonl"
    sp.write_text(_stream(_tool("Read", file_path="a.py")))
    shard = tmp_path / "ground.stream.b1.jsonl"
    shard.write_text(_stream(_tool("Read", file_path="b.py")))
    led = write_ledger([sp, shard], tmp_path / "ground.ledger.json")
    data = json.loads((tmp_path / "ground.ledger.json").read_text())
    assert data["stats"] == {"files_read": 2, "searches": 0}
    assert led.files_read == ["a.py", "b.py"]


def test_write_ledger_none_when_no_stream(tmp_path):
    assert write_ledger([tmp_path / "missing.jsonl"], tmp_path / "out.json") is None


def test_run_local_citations_resolve_against_run_dir(tmp_path):
    from rfc_agent.gates import check_citations

    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/prd.md").write_text("line1\nline2\n")
    ok, warn, resolved = check_citations("per raw/prd.md:2", FAKE_REPO, "rfc.md", run_dir=tmp_path)
    assert ok == [] and warn == [] and resolved == 1
    bad, _, _ = check_citations("per raw/prd.md:99", FAKE_REPO, "rfc.md", run_dir=tmp_path)
    assert bad and "out of range" in bad[0]
    missing, _, _ = check_citations("per raw/nope.md:1", FAKE_REPO, "rfc.md", run_dir=tmp_path)
    assert missing and "does not exist" in missing[0]




def test_qualified_citation_gate():
    from rfc_agent.gates import check_citations_qualified

    bad = check_citations_qualified("see app/clients/lab_buddy_client.py:32", "findings.md")
    assert bad and "unqualified" in bad[0]
    assert check_citations_qualified("see svc_b/app/clients/lab_buddy_client.py:32", "f") == []




def test_qualify_citations_fixes_unique_owner(tmp_path):
    from rfc_agent.gates import qualify_citations

    (tmp_path / "labs_agg/app").mkdir(parents=True)
    (tmp_path / "labs_agg/app/handlers.py").write_text("x\n")
    text = "see app/handlers.py:1 and labs_agg/app/handlers.py:1"
    fixed_text, fixed, unresolved = qualify_citations(text, tmp_path, ["labs_agg", "svc_a"])
    assert fixed == 1 and unresolved == []
    assert fixed_text.count("labs_agg/app/handlers.py:1") == 2


def test_qualify_citations_uses_the_documents_own_qualified_form(tmp_path):
    """A bare citation whose basename the SAME doc already qualifies exactly once resolves to that path,
    even when the filesystem is ambiguous — the doc's own citation was verified, a basename search is a
    guess. Real failure this fixes: bare `order_manager.rb:1787` alongside 6 qualified uses of
    svc_d/app/services/order_module/order_manager.rb, with a second order_manager.rb elsewhere on disk."""
    from rfc_agent.gates import qualify_citations

    for sub in ("app/services/order_module", "app/services/lead_module"):
        (tmp_path / "svc_d" / sub).mkdir(parents=True)
        (tmp_path / "svc_d" / sub / "order_manager.rb").write_text("x\n" * 2000)
    text = (
        "- reschedule at svc_d/app/services/order_module/order_manager.rb:1172\n"
        "- the predicate allows it — order_manager.rb:1787\n"
    )
    fixed_text, fixed, unresolved = qualify_citations(text, tmp_path, ["svc_d"])
    assert fixed == 1 and unresolved == []
    assert "svc_d/app/services/order_module/order_manager.rb:1787" in fixed_text


def test_self_qualification_refuses_when_the_doc_is_itself_ambiguous(tmp_path):
    """Two different qualified paths for one basename = the doc has not established which; fall through
    to the existing filesystem logic rather than picking one."""
    from rfc_agent.gates import qualify_citations

    for repo in ("svc_d", "svc_a"):
        (tmp_path / repo / "app").mkdir(parents=True)
        (tmp_path / repo / "app" / "order.rb").write_text("x\n" * 20)
    text = "svc_d/app/order.rb:3 and svc_a/app/order.rb:4 and bare order.rb:5\n"
    _t, fixed, unresolved = qualify_citations(text, tmp_path, ["svc_d", "svc_a"])
    assert fixed == 0
    assert unresolved and "ambiguous" in unresolved[0]


def test_qualify_citations_ambiguous_left_for_gate(tmp_path):
    from rfc_agent.gates import qualify_citations

    for r in ("labs_agg", "svc_a"):
        (tmp_path / r / "app").mkdir(parents=True)
        (tmp_path / r / "app/handlers.py").write_text("x\n")
    _, fixed, unresolved = qualify_citations("see app/handlers.py:1", tmp_path, ["labs_agg", "svc_a"])
    assert fixed == 0
    assert unresolved and "ambiguous" in unresolved[0]


def test_witnessed_accepts_unprefixed_form():
    led = build_ledger(_stream(_tool("Read", file_path="app/modules/x.py")))
    assert led.witnessed("svc_b/app/modules/x.py")  # qualified later, seen unqualified


def test_qualify_citations_line_context_tiebreak(tmp_path):
    from rfc_agent.gates import qualify_citations

    for r in ("lab_buddy", "svc_a"):
        (tmp_path / r / "app/constants").mkdir(parents=True)
        (tmp_path / r / "app/constants/cache.py").write_text("x\n")
    text = (
        "- `app/constants/cache.py:1` — TTL, see lab_buddy/app/managers/x.py:5\n"
        "- bare app/constants/cache.py:1 no hint\n"
    )
    fixed_text, fixed, unresolved = qualify_citations(text, tmp_path, ["lab_buddy", "svc_a"])
    assert fixed == 1 and "lab_buddy/app/constants/cache.py:1" in fixed_text
    assert unresolved and "ambiguous" in unresolved[0]  # the hint-less one stays for the gate


def test_subagent_tool_calls_are_witnessed():
    """Parallel-subagent scanning (GROUND/REVIEW fan-out) must not weaken the anti-fabrication gate:
    subagent messages carry parent_tool_use_id, and build_ledger keys on tool_use blocks regardless — so
    a file a SUBAGENT read is witnessed exactly like one the parent read. Verified live 2026-07-28."""
    s = _stream(
        _tool("Agent", _id="a1", description="scan svc_d"),
        {"type": "assistant", "parent_tool_use_id": "a1", "message": {"content": [
            {"type": "tool_use", "id": "s1", "name": "Read",
             "input": {"file_path": "svc_d/app/models/order.rb"}}]}},
        {"type": "user", "parent_tool_use_id": "a1", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "s1", "content": "42: enum status"}]}},
    )
    led = build_ledger(s)
    assert led.witnessed("svc_d/app/models/order.rb"), "subagent reads must witness"
    assert led.tool_counts.get("Agent") == 1 and led.tool_counts.get("Read") == 1


def test_repo_file_named_like_own_artifact_still_witnesses():
    """Field incident 2026-08-20 (records-import run, 4 identical GROUND failures, ~$15): bare
    substring matching classified the REAL repo file family_linking_plan.md as the run's own
    plan.md, so every read receipt was discarded and the gate's 're-read it' remedy livelocked.
    Component-bounded matching keeps such reads as witnesses."""
    incident = "patient_service_torpedo/family_linking/family_linking_plan.md"
    led = build_ledger(_stream(_tool("Read", file_path="/x/code/" + incident)))
    assert led.witnessed(incident), "a name that merely ENDS like an own artifact must witness"
    for collision in (
        "svc_d/app/models/security_findings.rb",     # old bare 'findings'
        "svc_a/app/services/myreview.py",             # old bare 'review'
        "payment_engine/app/models/general_ledger.rb", # old bare 'ledger'
    ):
        led = build_ledger(_stream(_tool("Read", file_path="/x/code/" + collision)))
        assert led.witnessed(collision), collision


def test_own_artifacts_still_excluded_under_boundary_matching():
    """The tightening must not weaken the anti-self-witness protection: every real own artifact —
    relative or absolute, read or heredoc-written — stays excluded."""
    for own in ("out/detail/plan.md", "out/rfc.md", "out/contract.md", "findings/findings.md",
                "review/persona_skeptic.md", "guidance/generate.md", "raw/prd.md",
                "extract/requirements.json", "/abs/run/dir/findings/evidence.md",
                "logs/ground.stream.jsonl", "copied_stream.jsonl", "logs/x.ledger.json"):
        led = build_ledger(_stream(_tool("Read", _id="r", file_path=own),
                                   _result("r", "- svc_d/app/models/order.rb:42")))
        assert not led.witnessed("svc_d/app/models/order.rb"), own


def test_search_pattern_word_collisions_no_longer_discard_witnesses():
    """A Grep PATTERN containing a word like 'reviewed_by' tripped the old bare 'review' token and
    silently discarded the interaction — pattern text is not a path reference."""
    led = build_ledger(_stream(
        _tool("Grep", _id="g1", pattern="reviewed_by", path="/x/code/svc_d"),
        _result("g1", "svc_d/app/models/slot_booking.rb:117: reviewed_by"),
    ))
    assert led.witnessed("svc_d/app/models/slot_booking.rb")


def test_witness_gate_names_misclassification_instead_of_futile_reread(tmp_path):
    """The deliberate residue: a repo file EXACTLY named like an own artifact (docs/plan.md) is
    still excluded — but the gate must say so and forbid the retry, never advise the re-read that
    can never succeed (the livelock that burned 4 attempts in the field)."""
    repo = tmp_path / "svc_d" / "docs"
    repo.mkdir(parents=True)
    (repo / "plan.md").write_text("x\n" * 5)
    rel = "svc_d/docs/plan.md"
    led = build_ledger(_stream(_tool("Read", file_path=str(tmp_path / rel))))
    assert not led.witnessed(rel)
    fails = check_citations_witnessed(f"see {rel}:3", tmp_path, led, "findings.md")
    assert fails and "MISCLASSIFIED" in fails[0] and "plan.md" in fails[0]
    assert "Read that file now" not in fails[0], "never advise the remedy that livelocks"
    # untouched files keep the standard remedy
    led2 = build_ledger(_stream())
    fails2 = check_citations_witnessed(f"see {rel}:3", tmp_path, led2, "findings.md")
    assert fails2 and "Read that file now" in fails2[0]
