from pathlib import Path

from rfc_agent.gates import check_citations

FAKE_REPO = Path(__file__).parents[1] / "fixtures" / "fake_repo"  # contains labs_agg/app/handlers.py (5 lines)


def test_citation_valid_resolves():
    failures, warnings, resolved = check_citations("see labs_agg/app/handlers.py:3", FAKE_REPO, "t")
    assert failures == [] and warnings == [] and resolved == 1


def test_citation_line_out_of_range_fails():
    failures, _, _ = check_citations("see labs_agg/app/handlers.py:999", FAKE_REPO, "t")
    assert failures and "out of range" in failures[0]


def test_citation_missing_file_fails():
    failures, _, _ = check_citations("see labs_agg/app/nope.py:1", FAKE_REPO, "t")
    assert failures and "does not exist" in failures[0]


def test_citation_nonlocal_repo_warns_not_fails():
    failures, warnings, resolved = check_citations("see ios_app/Sources/Main.swift:10", FAKE_REPO, "t")
    assert failures == [] and resolved == 0
    assert warnings and "non-local" in warnings[0]


def test_qualify_bare_citation_line_count_tie_break(tmp_path):
    """Ambiguous basenames resolved by line count: `booking.py:1210` can only live in a candidate that
    HAS 1210+ lines. Live driver 2026-08-07: svc_a holds 12 files named booking.py; shard shorthand
    produced 24 unrepairable bare citations and a 12-min LLM patch pass — exactly one candidate was
    long enough all along. Both-long stays unresolved (never guess)."""
    from rfc_agent.gates import qualify_citations

    root = tmp_path
    (root / "svc_a" / "app" / "repository").mkdir(parents=True)
    (root / "svc_a" / "app" / "managers").mkdir(parents=True)
    (root / "svc_a" / "app" / "repository" / "booking.py").write_text("x = 1\n" * 1500)
    (root / "svc_a" / "app" / "managers" / "booking.py").write_text("x = 1\n" * 200)

    text, fixed, unresolved = qualify_citations(
        "signing happens at `booking.py:1210` in the repository layer", root, ["svc_a"])
    assert fixed == 1 and "svc_a/app/repository/booking.py:1210" in text

    # line 50 fits BOTH candidates -> stays unresolved (ambiguity is honest, not guessed away)
    text2, fixed2, unresolved2 = qualify_citations("see `booking.py:50`", root, ["svc_a"])
    assert fixed2 == 0 and any("booking.py:50" in u for u in unresolved2)


def test_qualify_bare_citation_evidence_table_beats_geometry(tmp_path):
    """Evidence-table resolution: when the run's verified pack cites exactly one path with the bare
    basename, the shorthand means THAT path — what the scan established beats filesystem geometry."""
    from rfc_agent.gates import qualify_citations

    root = tmp_path
    for sub in ("repository", "managers"):
        (root / "svc_a" / "app" / sub).mkdir(parents=True)
        (root / "svc_a" / "app" / sub / "booking.py").write_text("x = 1\n" * 400)
    # both candidates are long enough -> line-count rule alone cannot resolve line 50…
    text, fixed, _ = qualify_citations("see `booking.py:50`", root, ["svc_a"])
    assert fixed == 0
    # …but the evidence table names exactly one of them
    text, fixed, _ = qualify_citations(
        "see `booking.py:50`", root, ["svc_a"],
        evidence_paths=["svc_a/app/repository/booking.py"])
    assert fixed == 1 and "svc_a/app/repository/booking.py:50" in text


def test_qualify_self_provenance_never_misqualifies_short_file(tmp_path):
    """v5 (2026-08-10): contract.md's only qualified order.py was routes/ (232 lines); bare
    `order.py:960` was rewritten to it — an out-of-range citation the HARNESS injected, which the
    gate then flagged. The self-provenance branch must line-check like every other branch."""
    from rfc_agent.gates import qualify_citations

    root = tmp_path
    (root / "svc_a" / "app" / "routes").mkdir(parents=True)
    (root / "svc_a" / "app" / "managers").mkdir(parents=True)
    (root / "svc_a" / "app" / "routes" / "order.py").write_text("x = 1\n" * 232)
    (root / "svc_a" / "app" / "managers" / "order.py").write_text("x = 1\n" * 6000)

    doc = "route at `svc_a/app/routes/order.py:73` -> manager (`order.py:960`)"
    text, fixed, _ = qualify_citations(doc, root, ["svc_a"])
    assert "svc_a/app/routes/order.py:960" not in text, "mis-qualified to a 232-line file"
    assert "svc_a/app/managers/order.py:960" in text  # line-count filter resolves it correctly


def test_qualify_own_set_ambiguity_resolved_by_line_count(tmp_path):
    """v5: rfc.md cited BOTH svc_a order.py files qualified, so the old exactly-one bail-out
    forfeited 33 resolvable citations to an LLM patch pass. Line-count filters the own set."""
    from rfc_agent.gates import qualify_citations

    root = tmp_path
    (root / "svc_a" / "app" / "routes").mkdir(parents=True)
    (root / "svc_a" / "app" / "managers").mkdir(parents=True)
    (root / "svc_a" / "app" / "routes" / "order.py").write_text("x = 1\n" * 232)
    (root / "svc_a" / "app" / "managers" / "order.py").write_text("x = 1\n" * 6000)

    doc = ("`svc_a/app/routes/order.py:73` and `svc_a/app/managers/order.py:988` both matter; "
           "the fan-out is at `order.py:4636`")
    text, fixed, _ = qualify_citations(doc, root, ["svc_a"])
    assert "svc_a/app/managers/order.py:4636" in text
    # line 100 fits both own candidates -> honestly unresolved, never guessed
    text2, fixed2, unresolved2 = qualify_citations(doc + " and `order.py:100`", root, ["svc_a"])
    assert any("order.py:100" in u for u in unresolved2)


def test_qualify_doc_cited_tie_break_beats_mock_geometry(tmp_path):
    """v5: a 9,688-line test mock (state_feeder_service tests/mock_service_clients/order.py)
    out-lengthed every real candidate, globally defeating the line-count tie-break. A candidate the
    document itself cites in qualified form beats a file nothing in the run ever named."""
    from rfc_agent.gates import qualify_citations

    root = tmp_path
    (root / "svc_a" / "app" / "managers").mkdir(parents=True)
    (root / "sfs" / "app" / "tests" / "mocks").mkdir(parents=True)
    (root / "svc_a" / "app" / "managers" / "order.py").write_text("x = 1\n" * 6000)
    (root / "sfs" / "app" / "tests" / "mocks" / "order.py").write_text("x = 1\n" * 9700)

    doc = "the manager `svc_a/app/managers/order.py` owns the flow; see `order.py:4636`"
    text, fixed, _ = qualify_citations(doc, root, ["svc_a", "sfs"])
    assert "svc_a/app/managers/order.py:4636" in text
    # a doc citing NEITHER stays honestly unresolved (both fit, no in-doc signal)
    _, fixed2, unresolved2 = qualify_citations("see `order.py:4636`", root, ["svc_a", "sfs"])
    assert fixed2 == 0 and unresolved2


def _lint_repo(tmp_path):
    import subprocess
    repo = tmp_path / "svc_a" / "app" / "managers"
    repo.mkdir(parents=True)
    (repo / "booking.py").write_text("class BookingManager:\n    def get_bookings_for_order_ids(self):\n        pass\n")
    (tmp_path / "svc_a" / "app" / "repository").mkdir(parents=True)
    (tmp_path / "svc_a" / "app" / "repository" / "booking.py").write_text(
        "def percentile_query():\n    pass\n" + "x = 1\n" * 50)
    (tmp_path / "svc_a" / "config_template.json").write_text('{"EXISTING_FLAG": true}')
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(cmd, cwd=tmp_path / "svc_a", check=True, capture_output=True)
    return tmp_path


def test_citation_multi_line_list_validates_every_endpoint():
    """`builder.py:56,68-76,3022-3031` used to validate ONLY line 56 — the tail's fabricated ranges
    rode through every gate and review caught six of them (2026-08-12). Every endpoint must check."""
    # first number valid, a tail endpoint out of range -> fail names the bogus line
    failures, _, _ = check_citations("see labs_agg/app/handlers.py:3,4-3022", FAKE_REPO, "t")
    assert failures and "3022" in failures[0] and "out of range" in failures[0]
    # all endpoints valid -> clean, every endpoint counted
    failures, _, resolved = check_citations("see labs_agg/app/handlers.py:1,2-4", FAKE_REPO, "t")
    assert failures == [] and resolved == 3
    # a defective path cited with a list must still produce ONE failure line, not one per endpoint
    failures, _, _ = check_citations("see labs_agg/app/nope.py:1,2-9", FAKE_REPO, "t")
    assert len(failures) == 1 and "does not exist" in failures[0]


def test_failure_classifier_maps_known_wordings():
    from rfc_agent.gates import classify_failure, failure_classes

    cases = {
        "declared output missing: out/rfc.md": "missing_output",
        "review BLOCKER: CHANGE-13 drops [:default]": "review_blocker",
        "findings.md: citation NOT witnessed in the scan trace (agent never touched it): x.py": "citation_witness",
        "out/rfc.md: bare citation with no path (x.py:3) — cite the full repo-qualified path": "citation_bare",
        "out/rfc.md: citation line out of range: a/b.py:900 (file has 12 lines)": "citation_resolve",
        "findings.md: absence claim looks FALSE — `foo` is claimed absent but exists at a/b.py": "absence_claim",
        "findings.md: 3 REQ(s) never mentioned — neither grounded with evidence nor listed": "req_coverage",
        "rfc.md: required section missing: 'Verdict'": "missing_section",
        "something entirely new": "other",
    }
    for text, cls in cases.items():
        assert classify_failure(text) == cls, text
    counts = failure_classes(list(cases))
    assert counts["citation_resolve"] == 1 and counts["other"] == 1


def test_note_inert_drain_dedupes_and_clears():
    from rfc_agent.gates import drain_inert, note_inert

    drain_inert()  # clean slate
    note_inert("citation_witness")
    note_inert("req_sufficiency")
    note_inert("citation_witness")
    assert drain_inert() == ["citation_witness", "req_sufficiency"]
    assert drain_inert() == []


def test_secrets_and_crossdoc_classify_above_other():
    from rfc_agent.gates import classify_failure

    assert classify_failure("rfc.md: possible secret(s) found — ['AKIA...']") == "secrets_found"
    assert classify_failure(
        "cross-doc contradiction: REQ-3 marked 'Covered' in rfc.md's coverage table but listed under "
        "Open Questions") == "cross_doc"


def test_mechanical_failures_never_classify_as_other():
    """Coherence contract between the two families over the same vocabulary (review finding #10):
    every wording engine._MECHANICAL_FAILURE_RE treats as patchable must carry a real class —
    'other' drifting is how the patch fast-path and the trace taxonomy disagree about one run."""
    from rfc_agent.engine import _MECHANICAL_FAILURE_RE
    from rfc_agent.gates import classify_failure

    corpus = [
        "out/rfc.md: bare citation with no path (x.py:3) — cite the full repo-qualified path",
        "out/rfc.md: unqualified citation (missing repo prefix): a/b.py:3",
        "out/rfc.md: citation line out of range: a/b.py:900 (file has 12 lines)",
        "out/rfc.md: cited file does not exist: a/b.py",
        "out/rfc.md: cited input file does not exist in the run dir: raw/x.md",
        "findings.md: input citation line out of range: raw/prd.md:99",
        "out/rfc.md: REQ-9 appears neither in its capability subsection ('booking') nor in Open Questions",
        "findings.md: citation NOT witnessed in the scan trace (agent never touched it): a/b.py",
        "cross-doc contradiction: REQ-1 marked 'Covered' in rfc.md's coverage table but listed under Open",
        "findings.md: absence claim looks FALSE — `foo` is claimed absent but exists at a/b.py",
    ]
    for f in corpus:
        assert _MECHANICAL_FAILURE_RE.search(f), f"corpus string must be mechanical: {f}"
        assert classify_failure(f) != "other", f"mechanical failure classified as other: {f}"


def test_output_shape_failures_classify_out_of_other():
    """0.24.56: the 0.24.34 output-shape lints fell into `other`, hiding the lint-churn class from
    aggregation (PROJ-3394-v6: the same shape lint failed gate:generate#3/#4/#5 in one revision loop,
    invisible as {other: 1} each time). Patterns quote the emitters verbatim."""
    from rfc_agent.gates import classify_failure, failure_classes

    shape = [
        "out/rfc.md:234: inline citation 'PatientApi.kt:29' in prose — move it to the Evidence line",
        "out/detail/plan.md: CHANGE-2 'what:' is 208 words (cap 150; spec is 2-6 lines)",
        "out/rfc.md: TL;DR is 131 words (cap 100; target <=3 sentences) — tighten",
        "out/rfc.md: TL;DR carries pipeline-internal identifier(s) ['REQ-3']",
        "out/rfc.md:88: composer self-narration in a deliverable ('this run verified')",
        "out/detail/plan.md: CHANGE-4 'what:' contains unresolved optionality ('optionally')",
        "out/rfc.md: 33,712 bytes exceeds the 30,720-byte ceiling (the compose target + ~20%)",
    ]
    assert all(classify_failure(f) == "output_shape" for f in shape)
    # neighbours keep their classes — output_shape must not shadow them
    assert classify_failure("out/detail/plan.md: bare citation with no path: patient.py:24") == "citation_bare"
    assert classify_failure("findings.md: citation line out of range: a.py:9 (file has 8)") == "citation_resolve"
    assert failure_classes([shape[0], shape[1], "review BLOCKER: x"]) == {
        "output_shape": 2, "review_blocker": 1}


def test_qualify_proximity_same_paragraph_unique_witness(tmp_path):
    """Paragraph-scoped proximity repair (2026-09-02, srp-bottomsheet-chat-history: 23 bare-citation
    gate failures, every one with its qualified form nearby): a bare citation whose basename was
    already cited IN FULL earlier in the same paragraph is shorthand for that path — the author's own
    local witness. UNIQUE only: a paragraph citing two different paths for the basename leaves the
    bare cite honestly unresolved (see test_qualify_own_set_ambiguity_resolved_by_line_count).
    Document-global self-provenance rightly bails when the doc names several paths for the basename;
    the paragraph usually does not have that ambiguity."""
    from rfc_agent.gates import qualify_citations

    root = tmp_path
    for sub in ("BookingAssistant", "BookingAssistantChatHistory"):
        d = root / "web" / "src" / sub
        d.mkdir(parents=True)
        (d / "index.js").write_text("x = 1\n" * 400)

    text = (
        "- The list container `web/src/BookingAssistantChatHistory/index.js:38` fetches on mount; "
        "the effect is keyed on loggedIn (`index.js:144`, no cache guard).\n"
        "\n"
        "- Unrelated paragraph citing the other file in full: "
        "`web/src/BookingAssistant/index.js:90`.\n"
    )
    out, fixed, unresolved = qualify_citations(text, root, ["web"])
    assert fixed == 1
    assert "(`web/src/BookingAssistantChatHistory/index.js:144`, no cache guard)" in out
    # the other paragraph's qualified citation is untouched
    assert "`web/src/BookingAssistant/index.js:90`" in out


def test_qualify_proximity_does_not_cross_paragraphs(tmp_path):
    """DESIRED: cross-paragraph proximity is NOT trusted — a same-basename citation in another
    paragraph may be a different file (index.js is the most common name in a JS repo). A bare cite
    whose only qualified form lives in a different paragraph at a DIFFERENT line stays unresolved
    (the exact-line rule below is the sanctioned cross-paragraph path)."""
    from rfc_agent.gates import qualify_citations

    root = tmp_path
    for sub in ("BookingAssistant", "BookingAssistantChatHistory"):
        d = root / "web" / "src" / sub
        d.mkdir(parents=True)
        (d / "index.js").write_text("x = 1\n" * 400)

    text = (
        "- Full cite here: `web/src/BookingAssistantChatHistory/index.js:38`.\n"
        "\n"
        "- Bare shorthand in another paragraph: `index.js:144` has no local witness.\n"
        "\n"
        "- And the doc also names `web/src/BookingAssistant/index.js:90` so global is ambiguous.\n"
    )
    out, fixed, unresolved = qualify_citations(text, root, ["web"])
    assert fixed == 0
    assert any("index.js:144" in u for u in unresolved)


def test_qualify_exact_line_agreement_across_paragraphs(tmp_path):
    """The real residual specimen (findings.md:63, 2026-09-02): bare `index.js:144-151` in one
    paragraph, `<full path>/index.js:144-151` in another. Same basename cited AT THE SAME LINE
    elsewhere in the doc = the same referent — repairable even across paragraphs, and immune to the
    global ambiguity that defeats self-provenance."""
    from rfc_agent.gates import qualify_citations

    root = tmp_path
    for sub in ("BookingAssistant", "BookingAssistantChatHistory"):
        d = root / "web" / "src" / sub
        d.mkdir(parents=True)
        (d / "index.js").write_text("x = 1\n" * 400)

    text = (
        "- Effect keyed on loggedIn: `web/src/BookingAssistantChatHistory/index.js:144-151`.\n"
        "\n"
        "- No caching exists; the action fires whenever the effect runs (`index.js:144-151`).\n"
        "\n"
        "- Global ambiguity: `web/src/BookingAssistant/index.js:90`.\n"
    )
    out, fixed, unresolved = qualify_citations(text, root, ["web"])
    assert fixed == 1
    assert "(`web/src/BookingAssistantChatHistory/index.js:144-151`)" in out


def test_qualify_suffix_repairs_partial_intra_repo_path(tmp_path):
    """Partial paths (`BookingAssistant/index.js:261`) rode through as 'non-local repo' WARNINGS —
    unverified citations in a gated artifact. A unique suffix match under the in-scope repos is a
    deterministic repair; an ambiguous suffix stays untouched for the gate's warning."""
    from rfc_agent.gates import qualify_citations

    root = tmp_path
    d = root / "web" / "src" / "containers" / "BookingAssistant"
    d.mkdir(parents=True)
    (d / "index.js").write_text("x = 1\n" * 400)

    out, fixed, _ = qualify_citations("see `BookingAssistant/index.js:261`", root, ["web"])
    assert fixed == 1 and "web/src/containers/BookingAssistant/index.js:261" in out

    # ambiguous suffix (two dirs share the tail) -> untouched
    d2 = root / "web" / "dweb" / "BookingAssistant"
    d2.mkdir(parents=True)
    (d2 / "index.js").write_text("x = 1\n" * 400)
    out2, fixed2, _ = qualify_citations("see `BookingAssistant/index.js:261`", root, ["web"])
    assert fixed2 == 0 and "see `BookingAssistant/index.js:261`" in out2


def test_qualify_suffix_and_proximity_respect_line_count(tmp_path):
    """Both new repairs inherit the hard line-count constraint: a candidate shorter than the cited
    line cannot be the referent (v5's mock-poisoning lesson applies to every repair branch)."""
    from rfc_agent.gates import qualify_citations

    root = tmp_path
    d = root / "web" / "src" / "BookingAssistant"
    d.mkdir(parents=True)
    (d / "index.js").write_text("x = 1\n" * 100)  # only 100 lines

    # suffix candidate too short for :261 -> no repair
    out, fixed, _ = qualify_citations("see `BookingAssistant/index.js:261`", root, ["web"])
    assert fixed == 0
    # proximity witness too short for :261 -> falls through (single fs candidate also too short)
    text = "- Full cite `web/src/BookingAssistant/index.js:90`; later bare `index.js:261`.\n"
    out2, fixed2, unresolved2 = qualify_citations(text, root, ["web"])
    assert fixed2 == 0 and any("index.js:261" in u for u in unresolved2)


def test_witness_trace_resolves_bare_citation_uniquely(tmp_path):
    """2026-09-03 (srp-bottomsheet-chat-history-v2): 23 bare cites sat in constraint paragraphs whose
    only qualified citation was a DIFFERENT basename, with several qualified paths for the bare one
    elsewhere in the doc — every document-geometry branch honestly declined, yet the authoring shard's
    Read ledger named the referent uniquely. The author's own trace beats document geometry."""
    from rfc_agent.gates import qualify_citations

    root = tmp_path
    for repo, lines in (("repo_a", 30), ("repo_b", 500)):
        d = root / repo / "app"
        d.mkdir(parents=True)
        (d / "actions.js").write_text("x\n" * lines)
    # document geometry is ambiguous: BOTH qualified forms appear (in other paragraphs)
    text = (
        "- **repo_a/app/actions.js:10** — one thing\n\n"
        "- **repo_b/app/actions.js:12** — another\n\n"
        "- the guard at `actions.js:423` is the only safety net\n"
    )
    # witness: the authoring run only ever Read repo_b's copy (absolute ledger path, as recorded)
    fixed_text, fixed, unresolved = qualify_citations(
        text, root, ["repo_a", "repo_b"],
        witness_paths=[str(root / "repo_b" / "app" / "actions.js")])
    assert fixed == 1 and "repo_b/app/actions.js:423" in fixed_text

    # witness ambiguous (both copies read) -> stays honestly unresolved (line-count already
    # rules out repo_a's 30-line file here, so make both fit by citing line 20)
    text2 = text.replace(":423", ":20")
    _t2, fixed2, _u2 = qualify_citations(
        text2, root, ["repo_a", "repo_b"],
        witness_paths=[str(root / "repo_a" / "app" / "actions.js"),
                       str(root / "repo_b" / "app" / "actions.js")])
    assert fixed2 == 0, "two witnessed candidates that both fit = genuine ambiguity, no guess"






def test_range_containment_resolves_bare_citation(tmp_path):
    """v8 ground (2026-09-08): 13 bare `index.js:144`-style citations rode a 14.7-min full-model
    revision while the doc cited each referent qualified as a SPAN (`.../index.js:116-160`) — the
    exact-line tier needs equality and declined them all. A line inside exactly one qualified span of
    the same basename names that span's file; two containing spans stay honestly ambiguous."""
    from rfc_agent.gates import qualify_citations

    for sub in ("a", "b"):
        d = tmp_path / "web" / sub
        d.mkdir(parents=True)
        (d / "index.js").write_text("x\n" * 400)
    # two qualified index.js paths (defeats self-provenance); only ONE span contains 144;
    # bare cite in a DIFFERENT paragraph (defeats paragraph proximity)
    text = ("- chrome `web/a/index.js:116-160` and tracking `web/b/index.js:300-320`\n\n"
            "- the dispatch at `index.js:144` is page-chrome\n")
    out, fixed, _ = qualify_citations(text, tmp_path, ["web"])
    assert fixed == 1 and "`web/a/index.js:144`" in out

    # both spans contain the line -> ambiguous, untouched
    text2 = ("- see `web/a/index.js:100-200` and `web/b/index.js:100-200`\n\n"
             "- bare `index.js:144`\n")
    out2, fixed2, unresolved2 = qualify_citations(text2, tmp_path, ["web"])
    assert fixed2 == 0 and any("index.js:144" in u for u in unresolved2)


