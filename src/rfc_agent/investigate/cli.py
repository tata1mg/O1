"""Investigate — lead: understand and ground. Design: docs/INVESTIGATE.md.

    python -m rfc_agent.investigate --run-dir <scope run dir> [--preset balanced|fast]
                               [--req-granularity grouped|fine] [--ask] [--yes] [--confirm-file] [--fresh]

1a  lead plans (reqs.json, flows.json + tracer plan) → Python checks (one fix) → renders flows.md, briefs
P1  pause if a scope repo has no snapshot, or the plan is still invalid (Scope's repos are final: no adding)
1b  tracers + UI readers in parallel; each checked as it exits (one fix round for must-fix items)
1c  lead resumed: integrate → understanding.md, open_questions.json; citation check (one fix)
P2  pause if --ask and there are open questions
Rerunning the same command resumes: finished steps and agents are kept.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import observe
from ..config import get_config
from ..legacy_names import migrate_run_dir
from ..scope import confirm
from ..scope import repos as scope_repos
from . import agent as ag
from . import checks, pauses, plan, repo_info, ui
from .config import resolve
from .progress import Progress

LEAD_TOOLS = ("Read", "Grep", "Glob", "Write", "Edit")
TRACER_TOOLS = ("Read", "Grep", "Glob", "Write", "Edit")
UI_TOOLS = ("Read", "Write", "Edit")


class Run:
    def __init__(self, run_dir: Path, cfg, s: dict, args):
        self.dir, self.cfg, self.s, self.args = run_dir, cfg, s, args
        self.work, self.facts = run_dir / "work", run_dir / "facts"
        self.work.mkdir(exist_ok=True)
        self.facts.mkdir(exist_ok=True)
        self.p = Progress(run_dir)
        self.state_path = self.work / "investigate.state.json"
        self.state = {} if args.fresh else _read(self.state_path) or {}
        self.agents: dict[str, dict] = self.state.get("agents", {})
        self.t0 = time.monotonic()
        self._lock = threading.RLock()

    # ---- bookkeeping ----
    def save(self) -> None:
        with self._lock:  # tracers and UI readers finish concurrently
            self.state["agents"] = self.agents
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)

    def record(self, r: ag.AgentResult, role: str, **extra) -> None:
        a = self.agents.setdefault(r.label, {"role": role, "runs": []})
        a["runs"].append(
            {
                "ok": r.ok,
                "seconds": r.seconds,
                "cost_usd": round(r.cost_usd, 4),
                "tool_calls": r.tool_calls,
                "error": r.error,
                "rate_limited": r.rate_limited,
                "stream": Path(r.stream).name,
            }
        )
        a.update(extra)
        self.save()
        self.p.agent(
            r.label,
            state="done" if r.ok else "failed",
            seconds=r.seconds,
            cost_usd=round(r.cost_usd, 3),
            tool_calls=r.tool_calls,
        )

    @property
    def scope(self) -> dict:
        return _read(self.dir / "scope.json") or {}

    def lead_spec(self, allow: tuple[str, ...]) -> ag.AgentSpec:
        return ag.AgentSpec(
            "lead",
            self.s["LEAD_MODEL"],
            self.s["LEAD_EFFORT"],
            LEAD_TOOLS,
            allow,
            timeout_s=int(self.s["LEAD_TIMEOUT_S"]),
            check_files=tuple(x for x in allow if x.endswith(".md")),  # understanding.md: checked at write time
        )

    def run_lead(self, prompt: str, allow: tuple[str, ...], step: str) -> ag.AgentResult:
        sid = self.state.get("lead_session")
        self.p.agent("lead", state="running")
        r = ag.run(self.lead_spec(allow), prompt, self.dir, self.cfg, resume=sid, on_event=self.p.tick, trace_step=step)
        if not r.ok and sid:  # session not resumable → fresh lead that re-reads the files
            r = ag.run(
                self.lead_spec(allow),
                "Start by reading work/reqs.json, work/flows.json and scope.json.\n\n" + prompt,
                self.dir,
                self.cfg,
                on_event=self.p.tick,
                trace_step=f"{step}-fresh",
            )
        if r.session_id:
            self.state["lead_session"] = r.session_id
            (self.dir / "logs" / "lead.session").write_text(r.session_id)
        self.record(r, "lead")
        return r

    # ---- 1a ----
    def step_1a(self) -> tuple[dict, dict, list[str]]:
        reqs, _ = plan.load_json(self.work / "reqs.json")
        flows, _ = plan.load_json(self.work / "flows.json")
        if self.state.get("1a") == "done" and reqs and flows:
            errs = plan.check(reqs, flows, self.scope, int(self.s["MAX_TRACERS"]))
            return reqs, flows, errs
        self.p.step("1a")
        self.p.milestone("start", "1a: the lead is planning (requirements, flows, tracer plan)")
        r = self.run_lead(self._prompt_1a(), ("work/reqs.json", "work/flows.json"), "1a")
        if not r.ok:
            raise SystemExit(f"investigate: the lead failed in 1a: {r.error}")
        self.state["1a_code_reads"] = _code_reads(Path(r.stream))
        reqs, e1 = plan.load_json(self.work / "reqs.json")
        flows, e2 = plan.load_json(self.work / "flows.json")
        errs = [e for e in (e1, e2) if e] or plan.check(reqs, flows, self.scope, int(self.s["MAX_TRACERS"]))
        observe.gate("plan", errs, kind="investigate_plan", stage="investigate")
        if errs:
            self.p.milestone("plan_fix", f"plan check: {len(errs)} problem(s) — sent back to the lead once")
            self.run_lead(
                "Your plan has these problems. Fix them in work/reqs.json / work/flows.json and "
                "reply done:\n- " + "\n- ".join(errs),
                ("work/reqs.json", "work/flows.json"),
                "1a-plan-fix",
            )
            reqs, e1 = plan.load_json(self.work / "reqs.json")
            flows, e2 = plan.load_json(self.work / "flows.json")
            errs = [e for e in (e1, e2) if e] or plan.check(reqs, flows, self.scope, int(self.s["MAX_TRACERS"]))
            observe.gate("plan", errs, kind="investigate_plan", round_label="after-fix", stage="investigate")
        self.state["1a"] = "done"
        self.save()
        if reqs and flows:
            self._render(reqs, flows)
            reads = self.state.get("1a_code_reads", 0)
            over = (
                f" · ⚠ {reads} code reads (limit {self.s['LEAD_CODE_READS']})"
                if reads > int(self.s["LEAD_CODE_READS"])
                else ""
            )
            self.p.milestone(
                "1a_done",
                f"Plan: {len(flows.get('flows') or [])} flows, "
                f"{len(flows.get('tracers') or [])} tracers ("
                + " · ".join(f"{t.get('id')} {','.join(t.get('flows') or [])}" for t in flows.get("tracers") or [])
                + f") · {len(reqs.get('requirements') or [])} REQs ({reqs.get('granularity')}){over}",
            )
        return reqs or {}, flows or {}, errs

    def _prompt_1a(self) -> str:
        files = ["- raw/prd.md — the PRD"]
        if (self.dir / "raw" / "jira.md").exists():
            files.append("- raw/jira.md — the Jira issue")
        files += [f"- raw/linked/{p.name}" for p in sorted((self.dir / "raw" / "linked").glob("*.md"))]
        if (self.dir / "raw" / "figma_index.md").exists():
            files.append("- raw/figma_index.md — the picked Figma screens (names, visible text)")
        repos = [
            f"- {r['repo']} — {r.get('source')} · {r.get('branch')}@{str(r.get('sha') or '')[:10]}"
            + (f" · _snap/{r['repo']}" if r.get("path") else " · NO SNAPSHOT")
            for r in self.scope.get("repos") or []
        ]
        g = self.s["REQ_GRANULARITY"]
        return plan.prompt(
            "lead_1a.md",
            INPUT_FILES="\n".join(files),
            REPOS="\n".join(repos),
            LEAD_CODE_READS=str(self.s["LEAD_CODE_READS"]),
            GRANULARITY=plan.GRANULARITY_TEXT[g],
            GRANULARITY_NAME=g,
            PARENT_FIELD=', "parent": "REQ-x or null"' if g == "fine" else "",
            PRESET=self.s["PRESET"],
            SPLIT_GUIDANCE=plan.SPLIT_TEXT[self.s["TRACER_SPLIT"]],
            MAX_TRACERS=str(self.s["MAX_TRACERS"]),
        )

    def _render(self, reqs: dict, flows: dict, ui_note: str = "") -> None:
        (self.work / "requirements.md").write_text(plan.render_requirements(reqs), encoding="utf-8")
        (self.work / "flows.md").write_text(
            plan.render_flows(flows, self.scope, self.s["PRESET"], int(self.s["MAX_TRACERS"]), ui_note),
            encoding="utf-8",
        )

    # ---- pause 1 ----
    def pause_1(self, reqs: dict, flows: dict, errs: list[str]) -> tuple[dict, dict]:
        if self.state.get("pause1") == "done":
            return reqs, flows
        outside, no_snap = plan.repos_needing_attention(flows, self.scope)
        if outside:  # Scope's repos are final: these hops are reported, never offered for adding
            self.p.milestone(
                "outside",
                "Not traced (outside the repos in scope): "
                + "; ".join(f"{r} ({', '.join(fs)})" for r, fs in outside.items()),
            )
        if not (no_snap or errs):
            self.state["pause1"] = "done"
            self.save()
            return reqs, flows
        self.p.step("pause1")
        items = [confirm.Item("retry", r, r, note=f"needed by {', '.join(fs)}") for r, fs in no_snap.items()]
        header = "── Investigate · Pause 1 ── flows and tracer plan (work/flows.md)\n" + "\n".join(
            f"  {t.get('id')}  {', '.join(t.get('flows') or [])}  {' → '.join(t.get('repos') or [])}"
            f"  “{t.get('why', '')}”"
            for t in flows.get("tracers") or []
        )
        if errs:
            header += (
                "\n  ⚠ the plan is still invalid after the lead's fix:\n    - "
                + "\n    - ".join(errs)
                + "\n  Give feedback (it goes to the lead once) or abort."
            )
        self.p.milestone(
            "pause1",
            f"Pause 1: {len(no_snap)} repo(s) without a snapshot{', plan invalid' if errs else ''} — waiting for you",
        )
        t = time.monotonic()
        ans = pauses.ask(
            1, self.dir, header, items, file_mode=self.args.confirm_file, accept_defaults=self.args.yes and not errs
        )
        self.state["pause1_wait_s"] = round(time.monotonic() - t, 1)
        retry = [it.key for it in ans["items"] if it.kind == "retry" and it.include]
        if retry:
            self._snapshot(retry)
        if ans["feedback"] or errs:
            note = ans["feedback"] or "(no feedback — fix the problems listed)"
            self.run_lead(
                "Feedback on your plan from the human:\n"
                + note
                + ("\nStill-open problems:\n- " + "\n- ".join(errs) if errs else "")
                + "\nRevise work/reqs.json and work/flows.json, then reply done.",
                ("work/reqs.json", "work/flows.json"),
                "pause1-feedback",
            )
            reqs, _ = plan.load_json(self.work / "reqs.json")
            flows, _ = plan.load_json(self.work / "flows.json")
            errs = plan.check(reqs or {}, flows or {}, self.scope, int(self.s["MAX_TRACERS"]))
            if errs:
                raise SystemExit("investigate: the plan is still invalid:\n- " + "\n- ".join(errs))
            self._render(reqs, flows)
        self.state["pause1"] = "done"
        self.save()
        return reqs, flows

    def _snapshot(self, names: list[str]) -> list[dict]:
        scope = self.scope
        root = Path(scope.get("repos_root") or "~").expanduser()
        self.p.milestone("snapshot", f"snapshotting {', '.join(names)} …")
        info = scope_repos.probe_all(names, root)
        recs = scope_repos.snapshot_all(
            names, root, self.dir / "_snap", branches={n: (info[n].branch, info[n].how) for n in names}
        )
        by = {r["repo"]: r for r in scope.get("repos") or []}
        for r in recs:
            by[r["repo"]] = r
        scope["repos"] = list(by.values())
        (self.dir / "scope.json").write_text(json.dumps(scope, indent=2), encoding="utf-8")
        for r in recs:
            self.p.milestone(
                "snapshot", f"{r['repo']}: {r['source']}" + (f" ({r.get('error')})" if r.get("error") else "")
            )
        return recs

    # ---- 1b ----
    def step_1b(self, reqs: dict, flows: dict) -> None:
        self.p.step("1b")
        spec = _ctx_spec(self.cfg)
        idx = repo_info.index_info(spec, flows, self.scope)
        (self.work / "index_age.json").write_text(json.dumps(idx, indent=2), encoding="utf-8")
        tracers = flows.get("tracers") or []
        readers = ui.plan_readers(self.dir, self.scope, flows, int(self.s["SCREENS_PER_READER"]))
        ui_note = f"{len(readers)} reader(s) · {sum(len(r['frames']) for r in readers)} screens" if readers else ""
        self._render(reqs, flows, ui_note)
        scope_repos = plan.scope_repos(self.scope)
        jobs = []
        for t in tracers:
            entry = repo_info.entry_prepass(self.dir, t, flows) if self.s["ENTRY_PREPASS"] else None
            brief = plan.render_brief(
                t, flows, reqs, self.scope, idx, str(entry.relative_to(self.dir)) if entry else None
            )
            bp = self.work / "briefs" / f"{t['id']}.md"
            bp.parent.mkdir(exist_ok=True)
            bp.write_text(brief, encoding="utf-8")
            jobs.append(("tracer", t, brief))
        for r in readers:
            path, node_ids, texts = ui.write_reader_inputs(self.dir, r)
            r.update(node_ids=node_ids, texts=texts, screens_file=str(path.relative_to(self.dir)))
            jobs.append(("ui", r, ""))
        todo = [j for j in jobs if self.agents.get(j[1]["id"], {}).get("status") not in ("ok", "flagged")]
        self.p.milestone(
            "1b_start",
            f"1b: {len(tracers)} tracer(s) + {len(readers)} UI reader(s) running"
            + (f" ({len(jobs) - len(todo)} already done)" if len(todo) < len(jobs) else ""),
        )
        for _kind, j, _ in todo:
            self.p.agent(j["id"], state="queued")
        with ThreadPoolExecutor(max_workers=max(1, len(todo))) as pool:
            list(pool.map(lambda job: self._run_job(job, flows, reqs, scope_repos), todo))
        self.state["1b"] = "done"
        self.save()

    def _run_job(self, job, flows: dict, reqs: dict, scope_repos: dict) -> None:
        kind, j, brief = job
        jid = j["id"]
        facts = f"facts/{jid}.md"
        if kind == "tracer":
            spec = ag.AgentSpec(
                jid,
                self.s["TRACER_MODEL"],
                self.s["TRACER_EFFORT"],
                TRACER_TOOLS,
                (facts,),
                call_budget=int(self.s["TRACER_CALL_BUDGET"]),
                timeout_s=int(self.s["TRACER_TIMEOUT_S"]),
                check_files=(facts,),  # citations checked at write time, while the code is still in front of it
            )
            entry_hint = (
                f"- facts/{jid}.entry.json lists where each hop's route and entry function are — start there."
                if (self.facts / f"{jid}.entry.json").exists()
                else ""
            )
            prompt = plan.prompt(
                "tracer.md",
                BRIEF=brief,
                ENTRY_HINT=entry_hint,
                CALL_BUDGET=str(spec.call_budget),
                FACTS_FILE=facts,
                TEMPLATE=(plan.PROMPTS / "facts_template.md").read_text(),
            )
            check = lambda: checks.check_tracer(j, flows, scope_repos, self.dir)  # noqa: E731
        else:
            spec = ag.AgentSpec(
                jid,
                self.s["UI_MODEL"],
                self.s.get("UI_EFFORT"),
                UI_TOOLS,
                (facts,),
                use_context=False,
                timeout_s=int(self.s["UI_TIMEOUT_S"]),
            )
            prompt = plan.prompt("ui_reader.md", SCREENS_FILE=j["screens_file"], FACTS_FILE=facts)
            check = lambda: checks.check_ui(j, flows, reqs, self.dir)  # noqa: E731
        self.p.agent(jid, state="running")
        r = ag.run(spec, prompt, self.dir, self.cfg, on_event=self.p.tick)
        if not r.ok:  # crash / timeout: one fresh retry
            self.record(r, kind)
            self.p.milestone("retry", f"{jid}: {r.error} — retrying once")
            r = ag.run(spec, prompt, self.dir, self.cfg, on_event=self.p.tick)
        self.record(r, kind)
        if not r.ok and not (self.dir / facts).exists():
            self._write_check(jid, {"must_fix": [f"agent failed: {r.error}"], "flags": []}, None, "untraced")
            self.p.milestone("untraced", f"{jid}: failed twice ({r.error}) — its flows are UNTRACED")
            return
        res = check()
        observe.gate(jid, res["must_fix"], res["flags"], kind=f"investigate_{kind}", stage="investigate")
        before = res
        if res["must_fix"]:
            self.p.agent(jid, state="fixing")
            self.p.milestone("check", f"{jid}: {len(res['must_fix'])} must-fix item(s) → sent back once")
            fix = ag.run(
                spec,
                "These problems were found in "
                + facts
                + ". Fix them in the file, then reply done:\n- "
                + "\n- ".join(res["must_fix"]),
                self.dir,
                self.cfg,
                resume=r.session_id,
                on_event=self.p.tick,
            )
            self.record(fix, kind)
            res = check()
            observe.gate(
                jid, res["must_fix"], res["flags"], kind=f"investigate_{kind}", round_label="after-fix",
                stage="investigate",
            )
        status = (
            "ok" if not res["must_fix"] and not res["flags"] else ("flagged" if not res["must_fix"] else "unverified")
        )
        self._write_check(jid, res, before if before is not res else None, status)
        bits = [
            f"{len(res['must_fix'])} unverified" if res["must_fix"] else "",
            f"{len(res['flags'])} flag(s)" if res["flags"] else "",
        ]
        cost = sum(x["cost_usd"] for x in self.agents[jid]["runs"])
        secs = sum(x["seconds"] for x in self.agents[jid]["runs"])
        self.p.milestone(
            "agent_done",
            f"{jid} done ({_mmss(secs)}, ${cost:.2f})"
            + (" · " + ", ".join(b for b in bits if b) if any(bits) else " · checks clean"),
        )

    def _write_check(self, jid: str, res: dict, before: dict | None, status: str) -> None:
        out = {"status": status, **res}
        if before:
            out["before_fix"] = before
        (self.facts / f"{jid}.check.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        self.agents.setdefault(jid, {"runs": []})["status"] = status
        self.save()

    # ---- 1c ----
    def step_1c(self, extra_note: str = "") -> None:
        self.p.step("1c")
        self.p.milestone("1c_start", "1c: the lead is integrating the facts")
        facts = sorted(p.name for p in self.facts.glob("T*.md"))
        uifacts = sorted(p.name for p in self.facts.glob("UI*.md"))
        checksf = sorted(p.name for p in self.facts.glob("*.check.json"))
        untraced = [
            p.stem.replace(".check", "")
            for p in self.facts.glob("*.check.json")
            if (_read(p) or {}).get("status") == "untraced"
        ]
        outside, _ = plan.repos_needing_attention(_read(self.work / "flows.json") or {}, self.scope)
        if outside:
            extra_note += (
                "NOT TRACED (outside the repos in scope): "
                + "; ".join(f"{r} ({', '.join(fs)})" for r, fs in outside.items())
                + " — say what each flow assumes about them (from the tracers' Out-of-scope calls); "
                "don't guess their code.\n"
            )
        prompt = plan.prompt(
            "lead_1c.md",
            FACTS_FILES=", ".join(f"facts/{f}" for f in facts) or "(none)",
            UI_FILES=", ".join(f"facts/{f}" for f in uifacts) or "(no UI readers)",
            CHECK_FILES=", ".join(f"facts/{f}" for f in checksf) or "(none)",
            UNTRACED=(
                f"UNTRACED (agent failed): {', '.join(untraced)} — treat their flows as open questions.\n"
                if untraced
                else ""
            )
            + extra_note,
        )
        allow = ("work/understanding.md", "work/open_questions.json")
        r = self.run_lead(prompt, allow, "1c")
        if not r.ok:
            raise SystemExit(f"investigate: the lead failed in 1c: {r.error}")
        broken = checks.check_understanding(self.dir, plan.scope_repos(self.scope))
        observe.gate("understanding", broken, kind="investigate_understanding", stage="investigate")
        if broken:
            self.p.milestone("check", f"understanding.md: {len(broken)} broken citation(s) → back to the lead once")
            self.run_lead(
                "Fix these citations in work/understanding.md, then reply done:\n- " + "\n- ".join(broken),
                allow,
                "1c-fix",
            )
        q = (_read(self.work / "open_questions.json") or {}).get("questions") or []
        text = (
            (self.work / "understanding.md").read_text(encoding="utf-8")
            if (self.work / "understanding.md").exists()
            else ""
        )
        cons = len(re.findall(r"^\s*[-*] ", _section(text, "Cross-flow constraints"), re.MULTILINE))
        self.p.milestone("1c_done", f"1c done: {cons} cross-flow constraint(s), {len(q)} open question(s)")
        self.state["1c"] = "done"
        self.save()

    # ---- pause 2 ----
    def pause_2(self, reqs: dict, flows: dict) -> None:
        if self.state.get("pause2") == "done":
            return
        qs = (_read(self.work / "open_questions.json") or {}).get("questions") or [] if self.s["ASK"] else []
        if not qs:
            self.state["pause2"] = "done"
            self.save()
            return
        self.p.step("pause2")
        items = [
            confirm.Item(
                "question", q.get("id"), q.get("question", "")[:110], note=f"blocks {', '.join(q.get('blocks') or [])}"
            )
            for q in qs
        ]
        self.p.milestone("pause2", f"Pause 2: {len(qs)} open question(s) — waiting for you")
        t = time.monotonic()
        ans = pauses.ask(
            2,
            self.dir,
            "── Investigate · Pause 2 ──",
            items,
            file_mode=self.args.confirm_file,
            accept_defaults=self.args.yes,
            questions={q.get("id"): q for q in qs},
        )
        self.state["pause2_wait_s"] = round(time.monotonic() - t, 1)
        if ans["answers"]:
            lines = ["# Answers from the human (treat as fact)", ""]
            for qid, a in ans["answers"].items():
                q = next((x for x in qs if x.get("id") == qid), {})
                lines += [f"## {qid} {q.get('question', '')}", a, ""]
            (self.work / "answers.md").write_text("\n".join(lines), encoding="utf-8")
            self.run_lead(
                "The human answered open questions — work/answers.md. Update work/understanding.md and "
                "work/open_questions.json (remove answered ones), then reply done.",
                ("work/understanding.md", "work/open_questions.json"),
                "pause2-answers",
            )
        self.state["pause2"] = "done"
        self.save()

    def finish(self) -> None:
        total = round(time.monotonic() - self.t0, 1)
        cost = sum(r["cost_usd"] for a in self.agents.values() for r in a.get("runs", []))
        throttled = sum(r.get("rate_limited", 0) for a in self.agents.values() for r in a.get("runs", []))
        summary = {
            "settings": self.s,
            "seconds_this_invocation": total,
            "cost_usd": round(cost, 3),
            "rate_limit_events": throttled,
            "agents": self.agents,
            "pause_wait_s": {k: self.state.get(k) for k in ("pause1_wait_s", "pause2_wait_s")},
            "lead_code_reads_1a": self.state.get("1a_code_reads"),
        }
        (self.dir / "logs" / "investigate.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self.p.step("done")
        self.p.milestone(
            "done",
            f"Investigate done: {_mmss(total)}, ${cost:.2f}"
            + (f", {throttled} rate-limit event(s)" if throttled else "")
            + f" → {self.work / 'understanding.md'}",
        )


# ---- helpers --------------------------------------------------------------------------------------


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _mmss(s: float) -> str:
    return f"{int(s // 60)}m{int(s % 60):02d}s"


def _ctx_spec(cfg):
    from ..scope.context import spec_of

    return spec_of(cfg)


def _section(text: str, name: str) -> str:
    m = re.search(rf"^#+ *{re.escape(name)}\s*$(.*?)(?=^#+ |\Z)", text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else ""


def _code_reads(stream: Path) -> int:
    n = 0
    try:
        for line in stream.read_text(encoding="utf-8").splitlines():
            if (
                '"tool_use"' in line
                and "_snap" in line
                and any(f'"name":"{t}"' in line.replace(" ", "") for t in ("Read", "Grep", "Glob"))
            ):
                n += 1
    except OSError:
        pass
    return n


@observe.entry("investigate")
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m rfc_agent.investigate",
                                description="Investigate: understand and ground.")
    p.add_argument("--run-dir", required=True, help="the Scope run folder")
    p.add_argument("--preset", choices=("balanced", "fast"))
    p.add_argument("--req-granularity", choices=("grouped", "fine"))
    p.add_argument("--ask", action="store_true", default=None, help="Pause 2 also asks the open questions")
    p.add_argument("--yes", action="store_true", help="accept pause defaults (an invalid plan still stops)")
    p.add_argument("--confirm-file", action="store_true",
                   help="pauses through files (what /rfc-agent:investigate uses)")
    p.add_argument("--fresh", action="store_true", help="ignore saved progress and redo every step")
    args = p.parse_args(argv)
    run_dir = Path(args.run_dir).expanduser().resolve()
    migrate_run_dir(run_dir)
    if not (run_dir / "scope.json").exists() or not (run_dir / "raw" / "prd.md").exists():
        print(f"investigate: {run_dir} has no scope.json / raw/prd.md — run Scope first", file=sys.stderr)
        return 1
    cfg = get_config()
    s = resolve(cfg, {"PRESET": args.preset, "REQ_GRANULARITY": args.req_granularity, "ASK": args.ask})
    run = Run(run_dir, cfg, s, args)
    (run.work / "investigate.config.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    run.p.milestone(
        "config",
        f"Investigate: preset {s['PRESET']}, requirements {s['REQ_GRANULARITY']}" + (", --ask" if s["ASK"] else ""),
    )
    reqs, flows, errs = run.step_1a()
    reqs, flows = run.pause_1(reqs, flows, errs)
    if run.state.get("1b") != "done" or any(
        a.get("status") not in ("ok", "flagged", "unverified", "untraced") for k, a in run.agents.items() if k != "lead"
    ):
        run.step_1b(reqs, flows)
    if run.state.get("1c") != "done":
        run.step_1c()
    run.pause_2(reqs, flows)
    run.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
