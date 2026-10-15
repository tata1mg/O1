"""Witness carry-over (0.22.59): a fresh revision inherits the compose session's reads, so a citation
carried over verbatim isn't re-accused of fabrication when the compose stream is gone."""

import json


def _stream(*paths: str) -> str:
    return "\n".join(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": "Read",
                                 "input": {"file_path": f"/repos/{p}"}}]},
    }) for i, p in enumerate(paths))


def test_fresh_attempt_wipes_the_carry_store_and_budget_state(tmp_path):
    """The runner's hygiene contract: a from-scratch rerun regenerates from zero, so witnesses (and
    consumed byte-budget warnings) accumulated by wiped attempts must not leak into it."""
    from rfc_agent.contracts import RunContext, Stage, StageResult
    from rfc_agent.runner import run_pipeline

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "generate.witness.txt").write_text("svc_d/app/models/order.py")
    (tmp_path / "logs" / "byte_budget.json").write_text('{"out/rfc.md": 2}')

    def engine(stage, ctx):
        p = ctx.path("out/rfc.md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        return StageResult(engine_desc="fake")

    stage = Stage(name="generate", requires=(), outputs=("out/rfc.md",),
                  build_prompt=lambda ctx: "p", gate=lambda ctx: [], engine=engine)
    run_pipeline([stage], RunContext(run_dir=tmp_path, inputs={}, config=None))
    assert not (tmp_path / "logs" / "generate.witness.txt").exists()
    assert not (tmp_path / "logs" / "byte_budget.json").exists()


