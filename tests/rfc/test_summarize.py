"""Summarize: the readable copy — written, skipped when up to date, rewritten when the RFC changes, one retry."""

from __future__ import annotations

import json

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, uuid
prompt = sys.stdin.read(); cwd = os.getcwd()
open(os.environ["FAKE_LOG"], "a").write("call\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": str(uuid.uuid4()), "mcp_servers": []}))
if "Write out/rfc.readable.md" in prompt and os.environ.get("FAKE_WRITE") == "1":
    src = open(os.path.join(cwd, "out", "rfc.md")).read()
    open(os.path.join(cwd, "out", "rfc.readable.md"), "w").write("# Patients — summary\n> TL;DR — short.\n" + src[:20])
print(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.01, "num_turns": 1}))
'''


def _setup(tmp_path, monkeypatch, write: bool):
    run = tmp_path / "run"
    (run / "out").mkdir(parents=True)
    (run / "out" / "rfc.md").write_text("# RFC: Patients\n" + "x" * 3000)
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")

    class Cfg:
        def get(self, k, d=None):
            return {"AGENT.CLAUDE_BIN": str(fake)}.get(k, d)

    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_WRITE", "1" if write else "0")
    monkeypatch.setattr("rfc_agent.summarize.cli.get_config", lambda: Cfg())
    return run, log


def test_readable_copy_written_then_kept_up_to_date(tmp_path, monkeypatch):
    run, log = _setup(tmp_path, monkeypatch, write=True)
    from rfc_agent.summarize import cli

    assert cli.main(["--run-dir", str(run)]) == 0
    out = run / "out" / "rfc.readable.md"
    assert out.read_text().startswith("# Patients — summary")
    summary = json.loads((run / "logs" / "summarize.json").read_text())
    assert summary["ok"] and summary["full_rfc_kb"] > summary["size_kb"]
    assert len(log.read_text().splitlines()) == 1
    assert cli.main(["--run-dir", str(run)]) == 0  # RFC unchanged → no agent call
    assert len(log.read_text().splitlines()) == 1
    (run / "out" / "rfc.md").write_text("# RFC: Patients v2\n")  # RFC changed (e.g. a Revise rerun) → rewritten
    assert cli.main(["--run-dir", str(run)]) == 0
    assert len(log.read_text().splitlines()) == 2


def test_readable_copy_one_retry_then_error(tmp_path, monkeypatch):
    run, log = _setup(tmp_path, monkeypatch, write=False)
    from rfc_agent.summarize import cli

    assert cli.main(["--run-dir", str(run)]) == 1
    assert len(log.read_text().splitlines()) == 2  # one retry, no more
    assert json.loads((run / "logs" / "summarize.json").read_text())["ok"] is False


def test_readable_needs_an_rfc(tmp_path):
    from rfc_agent.summarize import cli

    assert cli.main(["--run-dir", str(tmp_path)]) == 1
