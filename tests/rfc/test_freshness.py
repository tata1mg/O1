"""Green-skip freshness: a green stage may only skip when the inputs it was built from are
byte-identical now. Without this, `--stages extract --force` then a resume let a green GROUND carry
findings tagged against the OLD REQ numbering — the manifest-path twin of the fragment-cache
poisoning closed in 0.22.37. Also: ground's stale-checkout warning."""


from rfc_agent.contracts import Stage
from rfc_agent.runner import _inputs_digest


def _stage(name, requires=(), outputs=()):
    return Stage(name=name, requires=tuple(requires), outputs=tuple(outputs),
                 build_prompt=lambda ctx: "", gate=lambda ctx: [])


STAGES = [
    _stage("ingest", outputs=("raw/prd.md",)),
    _stage("extract", requires=("raw/prd.md",), outputs=("extract/requirements.json",)),
    _stage("ground", requires=("extract/requirements.json",), outputs=("findings/findings.md",)),
]


def _seed(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "extract").mkdir()
    (tmp_path / "raw/prd.md").write_text("the prd")
    (tmp_path / "raw/figma.md").write_text("## SCREEN-1: x")
    (tmp_path / "extract/requirements.json").write_text('{"requirements": []}')
    return tmp_path


def test_digest_changes_when_upstream_output_changes(tmp_path):
    run = _seed(tmp_path)
    before = _inputs_digest(STAGES, STAGES[2], run)  # ground's inputs
    (run / "extract/requirements.json").write_text('{"requirements": [{"id": "REQ-1"}]}')
    assert _inputs_digest(STAGES, STAGES[2], run) != before


def test_digest_covers_all_raw_files_not_just_declared(tmp_path):
    """extract reads EVERY raw file — an operator paste (raw/instrumentation.md) or a figma refetch
    must invalidate a green extract even though ingest only declares raw/prd.md."""
    run = _seed(tmp_path)
    before = _inputs_digest(STAGES, STAGES[1], run)  # extract's inputs
    (run / "raw/instrumentation.md").write_text("Event Name\tTrigger\n")
    assert _inputs_digest(STAGES, STAGES[1], run) != before


def test_digest_stable_when_nothing_changed(tmp_path):
    run = _seed(tmp_path)
    assert _inputs_digest(STAGES, STAGES[2], run) == _inputs_digest(STAGES, STAGES[2], run)
    # a stage's OWN output changing must not invalidate its own inputs
    before = _inputs_digest(STAGES, STAGES[2], run)
    (run / "findings").mkdir()
    (run / "findings/findings.md").write_text("# findings")
    assert _inputs_digest(STAGES, STAGES[2], run) == before


def test_first_stage_digest_is_constant(tmp_path):
    """ingest has no upstream — its digest never blocks a skip (its input is the remote PRD)."""
    run = _seed(tmp_path)
    d1 = _inputs_digest(STAGES, STAGES[0], run)
    (run / "raw/prd.md").write_text("changed content entirely")
    assert _inputs_digest(STAGES, STAGES[0], run) == d1


