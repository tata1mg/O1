/**
 * test-command — `/test` inside an Axon (Pi) session: the harness `testing` workflow, as the Claude Code plugin
 * `test-agent` runs it (/test-agent:test, /test-agent:execute, /test-agent:evaluate).
 *
 *   /test [--feature K] [--rfc-run-dir D] [--run-dir R] [--contract F] [--plan F] [--test-data F] [--figma F]
 *        phase A (generate → Kiwi → prerequisites), the review handoff, then phase B (execute) when the user says
 *        the cases are confirmed in Kiwi
 *   /test execute [web|backend|all] [--only-existing] [--run-dir R] [--env E] [--build B] [--headed] [--cases …] [--curl F]
 *   /test evaluate [--run-dir R]
 *   /test status [run dir]  ·  /test attach <run dir>  ·  /test doctor
 *
 * Like /rfc, nothing here talks to a model: the Claude Code commands are prompts that tell Claude which helper
 * to run and what to ask; here the same helpers (`python -m workflows.testing.{preflight,status,progress,
 * execute,evaluation}`) are called directly, the questions are Pi dialogs, and the harness runs as a detached
 * child writing `<run>/logs/run.<kind>.out`, so it outlives the session. `progress --watch` (the watcher the
 * Claude commands use) feeds the widget above the editor. Agent stages run on Axon (`--engine axon`) unless
 * the user picked engines. Login files, curls and ~/.tcms.conf are never read: only checked to exist.
 */

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import { argValue, checklist, childEnv, pidAlive, readNewLines, runCapture, splitArgs, tail, usesAxon, withEngine }
  from "./rfc-command.js";

const POLL_MS = 1000;
const WIDGET = "test-agent";
const MAX_LINES = 6;
const PID_FILE = "axon-test.pid";
const OTHER = "Other…";
const NO_WATCH_LIMIT_S = 7 * 24 * 3600; // one watcher for the whole launch: no 30-minute Monitor to re-arm

// ---- pure helpers (tested under node) -----------------------------------------------------------------

export const SUBCOMMANDS = ["execute", "evaluate", "status", "attach", "doctor", "help"];

/** `/test <args>` → {sub, argv}: sub is a subcommand, else "run" (phase A / resume). */
export function parseTestArgs(argv) {
  if (argv.length && SUBCOMMANDS.includes(argv[0])) return { sub: argv[0], argv: argv.slice(1) };
  if (argv[0] === "--help") return { sub: "help", argv: [] };
  return { sub: "run", argv };
}

/** The engine flags this invocation carries: the user's own, else `--engine axon`. */
export function engineArgs(argv) {
  const out = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--engine" || a === "--stage-engine") out.push(a, argv[++i] ?? "");
    else if (/^--(stage-)?engine=/.test(a)) out.push(a);
  }
  return out.length ? out : withEngine([]);
}

/** argv without the engine flags (the wrappers take only --engine, which engineFor gives them). */
export function withoutEngine(argv) {
  const out = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--engine" || argv[i] === "--stage-engine") i++;
    else if (!/^--(stage-)?engine=/.test(argv[i])) out.push(argv[i]);
  }
  return out;
}

/** The single engine name for `execute` / `evaluation` / `preflight check` (they take --engine only). */
export function engineFor(argv) {
  return argValue(engineArgs(argv), "--engine") || (usesAxon(engineArgs(argv)) ? "axon" : "");
}

const ICON = { ok: "✅", info: "ℹ️", warn: "⚠️", fail: "❌" };

/** A preflight check row → one checklist line. */
export function checkLine(c) {
  return `${ICON[c.level] || "•"} ${c.msg}${c.fix ? `  → ${c.fix}` : ""}`;
}

const STOP_CHECKS = ["config", "claude", "langfuse", "kiwi_client", "rfc", "prd", "rfc_run_dir"];

/** What the failed checks mean for the command (test.md §2.3). */
export function triageChecks(checks) {
  const failed = (checks || []).filter((c) => c.level === "fail");
  const kiwi = failed.find((c) => c.id === "kiwi");
  return {
    stop: failed.filter((c) => STOP_CHECKS.includes(c.id)),
    kiwiSetup: !!kiwi && /testing\.storage is 'json'/.test(kiwi.msg),
    kiwiConf: failed.some((c) => c.id === "kiwi_conf"),
    kiwiDown: !!kiwi && !/testing\.storage is 'json'/.test(kiwi.msg),
    warnings: (checks || []).filter((c) => c.level === "warn"),
  };
}

const STAGE_WHAT = {
  sources: "collecting inputs", generate_cases: "writing the suite, the long one, ~20 min",
  render_xlsx: "workbook", store_cases: "uploading to Kiwi", prerequisites: "what prestag needs",
  evaluate: "scoring the suite, ~15 min", derive_spec: "deriving the request spec", execute_be: "sending the requests",
  derive_ui: "writing the Playwright tests", execute_web: "running the browser tests",
  execute_ui_agent: "an agent drives a browser through each case", execute_app: "flows and agents on the emulator",
  publish_app: "recording the Maestro run",
};

const RE_EVENT = /^\[\d\d:\d\d:\d\d\] \[([a-z_]+)\] (.*)$/;

/** A `progress --watch` line → {type, stage, text} (type: started|passed|revising|failed|engine|progress|rearm|other). */
export function parseEvent(line) {
  const s = (line || "").trim();
  let m = /^WATCH rearm --from (\d+)/.exec(s);
  if (m) return { type: "rearm", offset: Number(m[1]) };
  m = /^PROGRESS ([a-z_]+) · (.*)$/.exec(s);
  if (m) return { type: "progress", stage: m[1], text: `⏳ ${m[1]} · ${m[2]}` };
  m = RE_EVENT.exec(s);
  if (!m) return { type: "other", text: s };
  const [, stage, rest] = m;
  if (rest.startsWith("STAGE started")) {
    return { type: "started", stage, text: `▶ ${stage}${STAGE_WHAT[stage] ? ` — ${STAGE_WHAT[stage]}` : ""}` };
  }
  if (rest.startsWith("gate PASSED")) return { type: "passed", stage, text: `✓ ${stage}` };
  if (rest.startsWith("gate FAILED") && /automatic revision next/.test(rest)) {
    const n = /(\d+) failure/.exec(rest);
    return { type: "revising", stage, text: `↻ ${stage} gate failed${n ? ` (${n[1]} failures)` : ""} — revising once automatically` };
  }
  if (rest.startsWith("gate FAILED")) return { type: "failed", stage, text: `✗ ${stage} gate failed` };
  if (rest.startsWith("ENGINE FAILURE")) return { type: "engine", stage, text: `✗ ${stage}: ${rest}` };
  return { type: "other", stage, text: rest };
}

const fmtMin = (s) => (s ? `${Math.round(s / 60)} min` : "");
const fmtCost = (c) => (c ? `$${Number(c).toFixed(2)}` : "");
const counts = (o) => Object.entries(o || {}).map(([k, v]) => `${v} ${k}`).join(" · ");

/** The review handoff (test.md §5), from `status --kiwi --json`. */
export function handoffText(st) {
  const c = st.cases || {};
  const out = [];
  out.push(`🔔 Cases are ready for review — ${st.run_dir}`);
  out.push(`1. Generated: ${c.total ?? "?"} cases in ${c.modules ?? "?"} modules — ${counts(c.execution)}; priority ${counts(c.priority)}.` +
    (st.duration_s ? ` Phase A took ${[fmtMin(st.duration_s), fmtCost(st.cost_usd)].filter(Boolean).join(", ")}.` : ""));
  if (st.eval) {
    const failed = (st.eval.failed_checks || []).map((x) => x.replaceAll("_", " ")).join(", ");
    out.push(`2. Quality: ${st.eval.overall}/5 (coverage ${st.eval.coverage}, quality ${st.eval.quality}); compliance ${st.eval.compliance}${failed ? ` — failed: ${failed}` : ""}.`);
  } else {
    out.push("2. Quality: not scored yet — /test evaluate rates it against the rubric and the 14 compliance checks (~15 min), any time.");
  }
  const s = st.store || {};
  if (s.total) {
    let line = `3. In Kiwi: ${s.total} cases in ${s.product} as PROPOSED — ${s.plan_url}`;
    if (s.protected) line += `. ${s.protected} were already reviewed and were left as they are (a suggestion comment shows what changed)`;
    const stale = Object.values((st.kiwi || {}).stale || {}).reduce((a, b) => a + (Array.isArray(b) ? b.length : Number(b) || 0), 0);
    if (stale) line += `. The plan also holds ${stale} cases from earlier runs this suite no longer has — they can be set to DISABLED`;
    out.push(line + ".");
  } else {
    out.push("3. In Kiwi: not uploaded yet.");
  }
  const p = st.prerequisites;
  if (p) {
    const top = (p.top_blockers || []).slice(0, 3).map((b) => `${b.id} ${b.entity} (${b.used_by} cases)`).join("; ");
    out.push(`4. Prerequisites: deploy ${(p.services || []).join(", ") || "—"}; ${counts(p.counts)}` +
      (p.secrets?.length ? `; secrets: ${p.secrets.join(", ")}` : "") +
      (p.fixtures_unknown_how ? `; ${p.fixtures_unknown_how} fixtures nobody knows how to create yet` : "") +
      (top ? `. Top blockers: ${top}` : "") + `. Full checklist: ${path.join(st.run_dir, "qa/prerequisites.md")}`);
  }
  if (st.test_data_supplied === false) {
    out.push(`5. Test data: the suite uses invented values. To use real ones, fill ${path.join(st.run_dir, "qa/test_data.template.yaml")} and re-run with --test-data <file> (reviewed cases stay protected), or correct the values in Kiwi.`);
  }
  if (st.xlsx) out.push(`Workbook: ${st.xlsx}`);
  out.push("Now: review the cases in Kiwi and set the ones you approve to CONFIRMED (reject with DISABLED). Only CONFIRMED cases run: " +
    "http cases against the API, web ui cases in a browser (Playwright), app ui cases from a Maestro run on your device. " +
    "manual cases are yours to run by hand in the Kiwi test run.");
  return out.join("\n");
}

/** `kiwi.by_status` → "CONFIRMED: 1 http · 8 ui" lines. */
export function kiwiStatusLines(kiwi) {
  return Object.entries((kiwi || {}).by_status || {}).map(([s, by]) => `${s}: ${counts(by)}`);
}

/** The kinds phase B can run, from `status --kiwi --json` → kiwi. */
export function kindRows(kiwi) {
  const k = kiwi || {};
  return [
    { kind: "backend", n: k.confirmed_http || 0, label: `Backend — ${k.confirmed_http || 0} of ${k.http_total || 0} CONFIRMED`, note: "needs a captured curl" },
    { kind: "web", n: k.confirmed_web_ui || 0, label: `Web UI — ${k.confirmed_web_ui || 0} CONFIRMED`, note: "Playwright; the env's base URL and a login" },
    { kind: "app", n: k.confirmed_app_ui || 0, label: `App UI — ${k.confirmed_app_ui || 0} CONFIRMED`, note: "a Maestro run from your device" },
  ];
}

/** The harness argv (after `uv run --project P`) for one launch. */
export function harnessCmd(kind, o) {
  const base = ["harness", "run", "testing", "--feature", o.feature, "--run-dir", o.runDir];
  if (o.rfcRunDir) base.push("--rfc-run-dir", o.rfcRunDir);
  const common = [...base, ...(kind === "generate" || kind === "store" ? [] : ["--env", o.env, "--build", o.build]), "--no-interactive"];
  const eng = o.engine || [];
  switch (kind) {
    case "generate":
      return [...common, ...(o.overrides || []), ...eng, ...(o.noStore ? ["--disable", "testing.store"] : [])];
    case "store":
      return [...common, "--stages", "store_cases,prerequisites", ...eng];
    case "backend":
      return [...common, "--stages", "derive_spec,execute_be", "--enable", "testing.execute", "--curl", o.curl, ...eng];
    case "web":
      return [...common, "--stages", "derive_ui,execute_web", "--enable", "testing.execute_web",
        ...(o.baseUrl ? ["--base-url", o.baseUrl] : []),
        ...(o.uiLogin ? ["--ui-login", o.uiLogin] : o.uiAuth ? ["--ui-auth", o.uiAuth] : []),
        ...(o.headed ? ["--enable", "testing.ui_headed"] : []), ...eng];
    case "app":
      return [...common, "--stages", "publish_app", "--enable", "testing.publish_app",
        "--maestro-report", o.report, "--maestro-flows", o.flows];
    default:
      throw new Error(`unknown launch kind ${kind}`);
  }
}

/** One results block (`results`, `results_web`, `results_app`) → report lines. */
export function resultLines(name, r) {
  if (!r) return [];
  const out = [`${name}: ${counts(r.tally) || "no results"}${r.run_url ? ` — ${r.run_url}` : ""}`];
  for (const x of r.not_passing || []) {
    out.push(`  ${x.extid} ${x.status}: ${String(x.detail || "").split("\n").find((l) => l.trim()) || ""}`.slice(0, 220));
  }
  const skipped = [];
  if (r.skipped_unconfirmed) skipped.push(`${r.skipped_unconfirmed} not CONFIRMED`);
  if (r.skipped_wrong_execution) skipped.push(`${r.skipped_wrong_execution} with the wrong exec: tag`);
  if (r.unattributed?.length) skipped.push(`${r.unattributed.length} no flow covered`);
  if (skipped.length) out.push(`  skipped: ${skipped.join(", ")}`);
  if (r.html_report) out.push(`  report: cd <project>/tools/ui-runner && npx playwright show-report ${r.html_report}`);
  return out;
}

/** The agent kinds' results block (`results_agent`, `results_android`) → report lines: tally, the table as it is. */
export function agentLines(name, r) {
  if (!r) return [];
  const parts = r.parts ? ` · ${r.parts_met} of ${r.parts} parts met` : "";
  const out = [`${name}: ${counts(r.tally) || "no results"}${parts}${r.run_url ? ` — ${r.run_url}` : ""}`];
  if (r.table) out.push("", r.table, "");
  if (r.not_executed?.length) out.push(`  not executed (no expected result): ${r.not_executed.join(", ")}`);
  if (r.drafts?.length) out.push(`  draft flows ready for convert-flows: ${r.drafts.join(", ")}`);
  for (const s of r.screenshots || []) out.push(`  ${s.extid} FAIL screenshot: ${s.path}`);
  return out;
}

/** What `execute ui-agent|android --pending` found → {web, flow, agent, notRun, lines}: the counts and one line per platform. */
export function pendingSummary(p) {
  const a = p.android;
  const web = (p.will_run || []).length;
  const flow = a ? (a.by_flow || []).length : 0;
  const agent = a ? (a.by_agent || []).length : 0;
  const notRun = a ? (a.not_run || []).length : 0;
  const lines = [];
  if (p.will_run) lines.push(`web: ${web} by agent${web > 5 ? ", in batches of 5" : ""}`);
  if (a) lines.push(`android: ${flow} by flow, ${agent} by agent, ${notRun} not run`);
  if (p.no_expected_result?.length) {
    lines.push(`not executed (no expected result; they stay IDLE in the Kiwi run): ${p.no_expected_result.join(", ")}`);
  }
  for (const n of (a && a.not_run) || []) lines.push(`  ${n.extid} not run: ${n.why}`);
  return { web, flow, agent, notRun, lines };
}

/** Hints for the usual phase B failures (test.md §6.4). */
export function failureHints(st) {
  const hints = [];
  const web = (st.results_web || {}).not_passing || [];
  const blocked = web.filter((x) => x.status === "BLOCKED");
  if (blocked.length && blocked.length >= web.length / 2 && blocked.some((x) => /net::ERR_|Timeout .*navigat|navigation timeout/i.test(x.detail || ""))) {
    hints.push("Most web cases are BLOCKED with a network error: the base URL is not reachable from here (VPN?).");
  }
  if (blocked.some((x) => /the login step failed/.test(x.detail || ""))) {
    hints.push("The login step failed: a wrong number/OTP in the login file, or the login page changed.");
  }
  const be = (st.stages || {}).execute_be;
  if (be && (be.failures || []).some((f) => /preflight failed/.test(String(f)))) {
    hints.push("execute_be's preflight failed: the target host was not reachable with that curl (VPN, an expired session) — run it again with a fresh curl.");
  }
  return hints;
}

/** The evaluate report (evaluate.md §4), from status.eval and qa/eval.json. */
export function evalText(st, evalJson) {
  const e = st.eval;
  if (!e) return "No score yet — the evaluate stage did not finish.";
  const out = [`Score ${e.overall}/5 — coverage ${e.coverage} (average of dimensions 1–5), quality ${e.quality} (average of 6–8); overall is the average of the two.`];
  out.push(`Compliance: ${e.compliance}`);
  const cc = (evalJson || {}).compliance_checks || {};
  for (const name of e.failed_checks || []) {
    const v = (cc[name] || {}).violations;
    out.push(`  ✗ ${name.replaceAll("_", " ")}${Array.isArray(v) ? ` — ${v.length} violation(s)` : ""}`);
  }
  if (e.missing_scenarios != null) out.push(`Missing scenarios found: ${e.missing_scenarios}`);
  out.push(`Full scorecard: ${path.join(st.run_dir, "qa/eval.json")}`);
  return out.join("\n");
}

/** `execute --print` output → {cmds, runDir, missing}. */
export function parsePrint(text) {
  const lines = (text || "").split("\n").map((l) => l.trim()).filter(Boolean);
  const miss = lines.map((l) => /missing (.+)$/.exec(l)).find(Boolean);
  const cmds = lines.filter((l) => / harness run testing /.test(l));
  const rd = cmds.length ? /--run-dir (\S+)/.exec(cmds[0]) : null;
  return { cmds, runDir: rd ? rd[1].replace(/^'|'$/g, "") : "", missing: miss ? miss[1].split(/, (?=--|qa\/|feature)/) : [] };
}

export const HELP = [
  "/test [--feature K] [--rfc-run-dir D] [--run-dir R] [--contract F] [--plan F] [--test-data F] [--figma F]",
  "      generate the QA suite from an RFC run → Kiwi (PROPOSED) → prerequisites; after your review, execute",
  "/test execute [web|backend|all] [--only-existing] [--run-dir R] [--env E] [--build B] [--headed] [--cases TC-…] [--curl F]",
  "      run the CONFIRMED cases again (web: the written tests at once, or write tests for new cases first)",
  "/test execute ui-agent|android [--run-dir R | --feature F [--kiwi-plan ID]] [--platform auto|web|android|both] [--env E] [--build B]",
  "      [--base-url U] [--ui-auth F] [--hosts H] [--cases …] [--headed] [--read-only] [--production] [--devices S,S] [--android-mode M]",
  "      an agent executes each CONFIRMED UI case: in a browser, and on an emulator (a validated Maestro flow when one covers it).",
  "      Flags only here; plain words (\"cart-sanity on prod, cases 173\") are understood in Claude Code's /test-agent:execute.",
  "/test evaluate [--run-dir R]      score the suite: the 8-dimension rubric and 14 compliance checks (~15 min)",
  "/test status [run dir]            where a run stands · /test attach <run dir>  follow a run started elsewhere",
  "/test doctor                      check Axon (sign-in, Pi, gateway models)",
  "Agents run on Axon unless you pass --engine / --stage-engine. The same commands in Claude Code: /test-agent:test, :execute, :evaluate.",
].join("\n");

// ---- one launch: a detached harness child, its progress watcher, its end ------------------------------

/** Why a launch that ended badly ended: the STOP / error lines of its log (a wrong parameter names itself there),
 *  else the exit code. null when it ended cleanly or the code is unknown (a run followed with /test attach). */
export function stopReason(logFile, code, lines = 400) {
  if (code === 0 || code === null || code === undefined) return null;
  const rows = tail(logFile, lines) || [];
  const hits = rows.filter((l) => /\[\w+\] STOP: |: error: |PIPELINE CONFIG ERROR: |^Traceback|Error: /.test(l));
  const why = hits.slice(-3).map((l) => l.replace(/^.*?(\[\w+\] STOP: )/, "").trim());
  return why.length ? why.join("\n") : `the process ended with exit ${code} — see ${logFile}`;
}

class Job {
  constructor({ dir, kind, label, child, pid, from, ctl, onDone, logFile }) {
    Object.assign(this, { dir, kind, label, child, pid, ctl, onDone, logFile });
    this.code = null;
    this.name = path.basename(dir);
    this.exited = false;
    this.lines = [];
    this.stage = "starting";
    this.progressAt = -1;
    this.t0 = Date.now();
    this.from = from || 0;
    if (child) child.on("exit", (code) => ((this.exited = true), (this.code = code)));
    this.startWatcher(this.from);
  }

  alive() {
    return this.child ? !this.exited : pidAlive(this.pid);
  }

  startWatcher(from) {
    const { uv, project } = this.ctl;
    const w = spawn(uv, ["run", "--project", project, "python", "-m", "workflows.testing.progress", this.dir, "--watch",
      "--from", String(from), "--max-s", String(NO_WATCH_LIMIT_S)], { env: childEnv(), stdio: ["ignore", "pipe", "ignore"] });
    let rest = "";
    w.stdout.on("data", (d) => {
      const parts = (rest + d).split("\n");
      rest = parts.pop();
      for (const l of parts) this.onEvent(parseEvent(l));
    });
    w.on("error", () => {});
    this.watcher = w;
  }

  stopWatcher() {
    try {
      this.watcher?.kill();
    } catch {}
    this.watcher = null;
  }

  push(text, { replaceProgress = false } = {}) {
    if (replaceProgress && this.progressAt >= 0 && this.progressAt === this.lines.length - 1) this.lines[this.progressAt] = text;
    else this.lines.push(text);
    while (this.lines.length > MAX_LINES) this.lines.shift();
    this.progressAt = replaceProgress ? this.lines.length - 1 : -1;
    this.render();
  }

  render() {
    const ui = this.ctl.ui();
    if (!ui) return;
    const mins = Math.floor((Date.now() - this.t0) / 60000);
    ui.setWidget(WIDGET, [`test · ${this.label} · ${this.stage} · ${mins} min`, ...this.lines.map((l) => "  " + l)]);
    ui.setStatus(WIDGET, `test: ${this.stage}`);
  }

  async onEvent(ev) {
    if (ev.type === "rearm") {
      this.stopWatcher();
      return this.startWatcher(ev.offset);
    }
    if (ev.type === "other") return;
    if (ev.type === "started") this.stage = ev.stage;
    if (ev.type === "passed") {
      const r = await this.ctl.uvRun(["python", "-m", "workflows.testing.status", this.dir, "--milestone", ev.stage]);
      const line = `✓ ${(r.out.trim() || ev.stage).split("\n").pop()}`;
      this.push(line);
      this.ctl.post(line);
      if (ev.stage === "derive_ui") {
        const st = await this.ctl.statusJson(this.dir);
        const sk = st?.ui_plan?.skipped || [];
        if (sk.length) this.ctl.post(`derive_ui left ${sk.length} case(s) for a person (they stay in the Kiwi run):\n` +
          sk.map((x) => `  ${x.extid}: ${x.reason}`).join("\n"));
      }
      return;
    }
    this.push(ev.text, { replaceProgress: ev.type === "progress" });
  }

  finish() {
    this.stopWatcher();
    const ui = this.ctl.ui();
    ui?.setWidget(WIDGET, undefined);
    ui?.setStatus(WIDGET, undefined);
    const why = this.logFile ? stopReason(this.logFile, this.code) : null;
    if (why) {
      ui?.notify(`test ${this.name}: stopped`, "error");
      this.ctl.post(`test: ${this.label} stopped.\n${why}`);
    }
    return this.onDone?.(this);
  }
}

// ---- the extension ------------------------------------------------------------------------------------

export default function make(pi, { project, uv = "uv" } = {}) {
  let latest = null;
  const jobs = new Map(); // run dir -> Job (one launch per run at a time)
  let timer = null;
  let lastDir = null;

  const ctl = {
    project,
    uv,
    ui: () => (latest && latest.hasUI ? latest.ui : null),
    post: (content) => pi.sendMessage({ customType: "test-agent", content, display: true }),
    uvRun: (args, cwd) => runCapture(uv, ["run", "--project", project, ...args], { cwd: cwd || latest?.cwd, env: childEnv() }),
    async json(args) {
      const r = await ctl.uvRun(args);
      try {
        return JSON.parse(r.out);
      } catch {
        return { _error: (r.err || r.out).trim().split("\n").pop() || `exit ${r.code}` };
      }
    },
    statusJson: (dir, kiwi = false) => ctl.json(["python", "-m", "workflows.testing.status", dir, ...(kiwi ? ["--kiwi"] : []), "--json"]),
  };
  const post = ctl.post;
  const helper = (mod, args) => ["python", "-m", `workflows.testing.${mod}`, ...args];

  const tick = async () => {
    for (const [dir, job] of jobs) {
      if (job.alive()) {
        job.render();
        continue;
      }
      jobs.delete(dir);
      try {
        await job.finish();
      } catch (e) {
        ctl.ui()?.notify(`test: ${e.message}`, "error");
      }
    }
    if (!jobs.size && timer) {
      clearInterval(timer);
      timer = null;
    }
  };

  /** Start `uv run --project P <argv>` detached, its output in <dir>/logs/<logName>. */
  function launch({ dir, kind, label, argv, logName, onDone }) {
    if (jobs.has(dir)) {
      ctl.ui()?.notify(`test: ${path.basename(dir)} already has a launch running here`, "warning");
      return null;
    }
    const logs = path.join(dir, "logs");
    fs.mkdirSync(logs, { recursive: true });
    let from = 0;
    try {
      from = fs.statSync(path.join(logs, "activity.log")).size;
    } catch {}
    const fd = fs.openSync(path.join(logs, logName), "w");
    const child = spawn(uv, ["run", "--project", project, ...argv], {
      cwd: latest?.cwd, env: childEnv(), detached: true, stdio: ["ignore", fd, fd],
    });
    fs.closeSync(fd);
    fs.writeFileSync(path.join(logs, PID_FILE), JSON.stringify({ pid: child.pid, kind, log: logName }));
    const job = new Job({ dir, kind, label, child, pid: child.pid, from, ctl, onDone, logFile: path.join(logs, logName) });
    jobs.set(dir, job);
    lastDir = dir;
    job.render();
    if (!timer) timer = setInterval(tick, POLL_MS);
    return job;
  }

  // ---- questions ----

  async function pick(title, options) {
    const ui = ctl.ui();
    if (!ui) return undefined;
    return ui.select(title, options);
  }
  async function ask(title, placeholder = "") {
    const ui = ctl.ui();
    if (!ui) return undefined;
    const v = await ui.input(title, placeholder);
    return v === undefined ? undefined : String(v).trim();
  }
  /** A pick from rows, or free text through "Other…". */
  async function pickOrType(title, rows, typeTitle) {
    const labels = rows.map((r) => r.label);
    const c = await pick(title, [...labels, OTHER]);
    if (c === undefined) return undefined;
    if (c === OTHER) return ask(typeTitle);
    return rows[labels.indexOf(c)]?.value;
  }
  const nonEmptyFile = (p) => {
    try {
      return fs.statSync(p).isFile() && fs.statSync(p).size > 0; // never read: it holds a login or a session
    } catch {
      return false;
    }
  };
  const expand = (p) => (p && p.startsWith("~/") ? path.join(process.env.HOME || "", p.slice(2)) : p);
  const readJson = (p) => {
    try {
      return JSON.parse(fs.readFileSync(p, "utf8"));
    } catch {
      return {};
    }
  };

  // ---- /test: phase A ----

  async function startOrResume(argv) {
    const eng = engineArgs(argv);
    const user = withoutEngine(argv);
    const given = (f) => {
      const v = argValue(user, f);
      return v && !v.startsWith("--") ? v : "";
    };
    let runDir = given("--run-dir");
    if (runDir) runDir = path.resolve(latest.cwd, expand(runDir));
    if (runDir && fs.existsSync(path.join(runDir, "manifest.json"))) return resume(runDir, eng);
    if (!runDir) {
      const filt = [...(given("--rfc-run-dir") ? ["--rfc-run-dir", expand(given("--rfc-run-dir"))] : []),
        ...(given("--feature") ? ["--feature", given("--feature")] : [])];
      const rows = await ctl.json(helper("preflight", ["runs", "--json", "--limit", "3", ...filt]));
      if (Array.isArray(rows) && rows.length) {
        const NEW = "Start a new run (regenerates the whole suite, ~30 min)";
        const labels = rows.map((r) => `${r.feature} · ${r.name.slice(0, 10)} — ${r.label}`);
        const c = await pick("Continue a testing run, or start a new one?", [...labels, NEW]);
        if (c === undefined) return;
        if (c !== NEW) return resume(rows[labels.indexOf(c)].path, eng);
      }
    }

    // inputs: RFC run first, then a feature key from that run only
    let rfcRun = given("--rfc-run-dir") ? path.resolve(latest.cwd, expand(given("--rfc-run-dir"))) : "";
    if (!rfcRun) {
      const rows = await ctl.json(helper("preflight", ["recent", "--json"]));
      if (!Array.isArray(rows) || !rows.length) return post("test: no RFC run found under RUNS_ROOT — pass --rfc-run-dir <rfc run>.");
      rfcRun = await pickOrType("Which RFC run?", rows.slice(0, 6).map((r) => ({ label: `${r.name} — ${r.title}`, value: r.path })),
        "RFC run folder (path)");
      if (!rfcRun) return;
      rfcRun = path.resolve(latest.cwd, expand(rfcRun));
    }
    let feature = given("--feature");
    if (!feature) {
      const rows = await ctl.json(helper("preflight", ["features", "--rfc-run-dir", rfcRun, "--json"]));
      feature = Array.isArray(rows) && rows.length
        ? await pickOrType("Feature key?", rows.map((r) => ({ label: `${r.key} — ${r.why}`, value: r.key })), "Feature key")
        : await ask("Feature key (names the Kiwi product, e.g. HDPO)");
      if (!feature) return;
    }
    const overrides = [];
    for (const f of ["--contract", "--plan", "--test-data", "--figma"]) if (given(f)) overrides.push(f, expand(given(f)));

    // preflight
    const engine = engineFor(argv);
    if (engine === "axon") {
      ctl.ui()?.setWorkingMessage?.("test: checking Axon…");
      const d = await ctl.uvRun(["python", "-m", "rfc_agent.axon_agent", "doctor", "--no-atlassian"]);
      ctl.ui()?.setWorkingMessage?.();
      if (d.code !== 0) return post(`test: not started — Axon preflight failed\n${(d.out + d.err).trim()}`);
    }
    let noStore = false;
    let verdict;
    for (;;) {
      ctl.ui()?.setWorkingMessage?.("test: preflight…");
      verdict = await ctl.json(helper("preflight", ["check", "--feature", feature, "--rfc-run-dir", rfcRun, ...overrides,
        ...(engine ? ["--engine", engine] : []), "--json"]));
      ctl.ui()?.setWorkingMessage?.();
      if (verdict._error) return post(`test: preflight could not run — ${verdict._error}`);
      post(`Preflight for ${feature}:\n${verdict.checks.map(checkLine).join("\n")}`);
      const t = triageChecks(verdict.checks);
      if (t.stop.length) return post(`test: not started. Fix first:\n${t.stop.map((c) => `  ${c.msg}${c.fix ? ` → ${c.fix}` : ""}`).join("\n")}`);
      if (t.kiwiConf) {
        return post("test: there is no Kiwi login. Create ~/.tcms.conf yourself ([tcms] url / username / password, then chmod 600 ~/.tcms.conf) — " +
          "never paste the password here — then run /test again.");
      }
      if (t.kiwiSetup) {
        const web = await ask("Kiwi is not set up on this machine. Kiwi web URL (e.g. https://kiwi.example.com)");
        const prefix = web && (await ask("Kiwi product prefix (e.g. Sandbox / )", "Sandbox / "));
        const user = web && (await ask("The agent's Kiwi username (empty to skip)"));
        if (!web || prefix === undefined) return;
        const r = await ctl.uvRun(helper("preflight", ["setup-kiwi", "--kiwi-web", web, "--product-prefix", prefix, ...(user ? ["--kiwi-user", user] : [])]));
        post((r.out + r.err).trim() || "wrote the TESTING Kiwi settings to config.json");
        continue;
      }
      if (t.kiwiDown) {
        const RETRY = "Retry (after connecting the VPN)";
        const LATER = "Generate now, store later";
        const c = await pick("Kiwi is unreachable — how do you want to go on?", [RETRY, LATER, "Stop"]);
        if (c === RETRY) continue;
        if (c !== LATER) return;
        noStore = true;
      }
      if (t.warnings.some((w) => w.id === "contract")) post("No API contract: backend cases come out manual and nothing is executable.");
      if (t.warnings.some((w) => w.id === "test_data")) post("No test data: the suite uses invented values; the prerequisites stage writes a template to fill.");
      break;
    }
    const res = verdict.resolved || {};
    feature = res.feature || feature;
    if ((res.existing_runs || []).length) {
      const ex = res.existing_runs[0];
      const CONT = `Continue ${ex.name} — ${ex.label}`;
      const c = await pick(`${feature} already has a run from this RFC — continue it?`,
        [CONT, "Start fresh (regenerates the suite, ~30 min; cases reviewed in Kiwi stay as they are)"]);
      if (c === undefined) return;
      if (c === CONT) return resume(ex.path, eng);
    }
    const dir = res.run_dir;
    if (!dir) return post("test: preflight gave no run folder");
    launchGenerate({ dir, feature, rfcRunDir: rfcRun, overrides, engine: eng, noStore });
  }

  function launchGenerate(o) {
    const job = launch({
      dir: o.dir, kind: "generate", label: `${path.basename(o.dir)} · generate`,
      argv: harnessCmd("generate", { feature: o.feature, rfcRunDir: o.rfcRunDir, runDir: o.dir, overrides: o.overrides, engine: o.engine, noStore: o.noStore }),
      logName: "run.generate.out", onDone: () => afterPhaseA(o),
    });
    if (job) post(`Generating the suite in the background — about 30 minutes: ${o.dir}\nProgress shows above the editor; each stage is posted when it finishes. ` +
      `It keeps running if you quit Axon — /test attach ${o.dir} picks it up again.`);
  }

  function launchStore(o) {
    const job = launch({
      dir: o.dir, kind: "store", label: `${path.basename(o.dir)} · store`,
      argv: harnessCmd("store", { feature: o.feature, rfcRunDir: o.rfcRunDir, runDir: o.dir, engine: o.engine }),
      logName: "run.store.out", onDone: () => afterPhaseA(o),
    });
    if (job) post(`Uploading to Kiwi and writing the prerequisites: ${o.dir}`);
  }

  function runInfo(dir) {
    const src = readJson(path.join(dir, "qa", "sources.json"));
    return { dir, feature: src.feature || "", rfcRunDir: src.rfc_run_dir || "" };
  }

  /** A run folder that exists: go by `status --json` → next.step (test.md §1). */
  async function resume(dir, eng) {
    const o = { ...runInfo(dir), engine: eng, overrides: [] };
    const st = await ctl.statusJson(dir);
    if (st._error) return post(`test: could not read ${dir} — ${st._error}`);
    return goNext(st, o);
  }

  async function goNext(st, o) {
    const step = st.next?.step;
    if (step === "start" || step === "resume_generate") {
      if (!o.feature) return post(`test: ${o.dir} has no qa/sources.json yet — start it again with /test --rfc-run-dir <rfc run> --feature <key>`);
      return launchGenerate(o);
    }
    if (step === "fix") return offerFix(st, o);
    if (step === "store") return storeAgain(o);
    if (step === "review") return handoff(o);
    if (step === "execute") return phaseB(o);
    if (step === "done") return report(o.dir);
    return post(`test: ${o.dir} — next step "${step}" is not one /test knows`);
  }

  async function offerFix(st, o) {
    const why = st.next?.why || {};
    const stage = st.next?.stage || "";
    post(why.message || `${stage} stopped.`);
    if (why.retry) {
      const c = await pick(`${stage} stopped — resume?`, [`Resume from ${stage}`, "Later"]);
      if (c && c.startsWith("Resume")) return stage === "store_cases" ? launchStore(o) : launchGenerate(o);
    } else {
      const f = (st.stages?.[stage]?.failures || []).slice(0, 8);
      if (f.length) post(`${stage} gate failures:\n${f.map((x) => `  ${typeof x === "string" ? x : JSON.stringify(x)}`).join("\n")}`);
      const c = await pick(`${stage} failed its gate — what now?`, ["Rerun", "Stop"]);
      if (c === "Rerun") return launchGenerate(o);
    }
    post(`Continue any time with /test --run-dir ${o.dir}`);
  }

  async function storeAgain(o) {
    const v = await ctl.json(helper("preflight", ["check", "--feature", o.feature, ...(o.rfcRunDir ? ["--rfc-run-dir", o.rfcRunDir] : []),
      "--engine", engineFor(o.engine) || "claude-code", "--json"]));
    if ((v.checks || []).some((c) => c.id === "kiwi" && c.level === "fail")) {
      return post(`Kiwi is still unreachable. Resume later with /test --run-dir ${o.dir}`);
    }
    launchStore(o);
  }

  async function afterPhaseA(o) {
    const st = await ctl.statusJson(o.dir);
    if (st._error) return post(`test: could not read the run's status — ${st._error}`);
    const step = st.next?.step;
    if (step === "fix" && st.next?.stage === "store_cases" && st.next?.why?.kind === "connection") {
      post(st.next.why.message || "Kiwi dropped during the upload.");
      const c = await pick("Retry the upload to Kiwi?", ["Retry the upload", "Later"]);
      return c === "Retry the upload" ? storeAgain(o) : post(`Resume later with /test --run-dir ${o.dir}`);
    }
    if (step === "store") {
      const c = await pick("The suite is generated but not in Kiwi yet. Upload it now?", ["Upload to Kiwi", "Later"]);
      return c === "Upload to Kiwi" ? storeAgain(o) : post(`Upload later with /test --run-dir ${o.dir}`);
    }
    return goNext(st, o);
  }

  // ---- the review handoff (test.md §5) ----

  async function handoff(o) {
    const st = await ctl.statusJson(o.dir, true);
    if (st._error) return post(`test: could not read the run's status — ${st._error}`);
    post(handoffText(st));
    return readyQuestion(o);
  }

  async function readyQuestion(o) {
    for (;;) {
      const GO = "I've confirmed — execute";
      const CHECK = "Check Kiwi status";
      const c = await pick("Ready to execute the confirmed cases?", [GO, CHECK, "Later"]);
      if (c === GO) return phaseB(o);
      if (c !== CHECK) return post(`Come back any time with /test --run-dir ${o.dir} — it lands on this question.`);
      const st = await ctl.statusJson(o.dir, true);
      post(st.kiwi?.error ? `Kiwi is unreachable: ${st.kiwi.error}` : kiwiStatusLines(st.kiwi).join("\n") || "No cases in Kiwi.");
    }
  }

  // ---- phase B (test.md §6) ----

  async function phaseB(o) {
    let st;
    for (;;) {
      st = await ctl.statusJson(o.dir, true);
      if (!st.kiwi?.error && !st._error) break;
      post(`Kiwi is unreachable — every kind needs it. ${st.kiwi?.error || st._error}`);
      const c = await pick("Kiwi is unreachable", ["Retry", "Stop"]);
      if (c !== "Retry") return;
    }
    const kiwi = st.kiwi || {};
    if ((kiwi.by_status || {}).conflict) {
      post("Some cases carry two exec: tags from an older upload; uploading again (/test --run-dir … → upload) fixes the ones nobody reviewed.");
    }
    const rows = kindRows(kiwi);
    post(`backend: ${kiwi.confirmed_http || 0} of ${kiwi.http_total || 0} CONFIRMED · web UI: ${kiwi.confirmed_web_ui || 0} CONFIRMED · app UI: ${kiwi.confirmed_app_ui || 0} CONFIRMED`);
    const avail = rows.filter((r) => r.n > 0);
    if (!avail.length) {
      post(`Nothing would run: no case is CONFIRMED yet. ${kiwiStatusLines(kiwi).join(" · ")}`);
      return readyQuestion(o);
    }
    const on = await checklist(ctl.ui(), "Which kinds should run?", avail.map((r) => ({ label: r.label, note: r.note, on: true })));
    const kinds = avail.filter((_, i) => on[i]).map((r) => r.kind);
    if (!kinds.length) return post("Nothing picked.");

    // environment and build
    const appOnly = kinds.length === 1 && kinds[0] === "app";
    let env;
    let baseUrl = "";
    if (appOnly) env = await ask("Which device? (e.g. emulator-5554)");
    else {
      const envs = await ctl.json(helper("preflight", ["envs", "--json"]));
      if (Array.isArray(envs) && envs.length) {
        env = await pickOrType("Which environment?", envs.map((e) => ({ label: `${e.env} — ${e.base_url || "no base URL"}`, value: e.env })), "Environment name");
        baseUrl = (envs.find((e) => e.env === env) || {}).base_url || "";
      } else {
        env = await ask("Environment name (none are configured yet)");
        const list = env && (await ask("To keep a pick-list: all environment names, comma-separated (empty to skip)"));
        const tpl = list && (await ask("Their base-URL template", "https://{env}.example.com"));
        if (list && tpl) {
          const r = await ctl.uvRun(helper("preflight", ["setup-web", "--environments", list, "--base-url-template", tpl]));
          post((r.out + r.err).trim());
          baseUrl = tpl.replace("{env}", env);
        }
      }
    }
    if (!env) return;
    const build = (await ask("Build (a version or git sha)", "unknown")) || "unknown";
    const common = { feature: o.feature, rfcRunDir: o.rfcRunDir, runDir: o.dir, env, build, engine: o.engine };

    // inputs, only for the kinds chosen; login files and curls are checked to exist, never read
    const queue = [];
    if (kinds.includes("backend")) {
      const curl = expand(await ask("Path of a captured curl against the target host (DevTools → Network → Copy as cURL → a file)"));
      if (curl && nonEmptyFile(curl)) queue.push({ kind: "backend", ...common, curl });
      else post("Backend skipped: no readable curl file at that path.");
    }
    if (kinds.includes("web")) {
      const web = await webInputs(o, baseUrl);
      if (web) queue.push({ kind: "web", ...common, ...web });
    }
    if (kinds.includes("app")) {
      post("App UI runs on your device. If you have not run it yet (the flows are on druid's AND-106907 branch):\n" +
        "  git -C ~/o_1/druid worktree add ~/o_1/druid-maestro origin/AND-106907\n" +
        "  cd ~/o_1/druid-maestro && maestro test .maestro/flows/<suite> --format junit --output report.xml");
      const report = expand(await ask("Path of the Maestro JUnit report"));
      const flows = report && expand(await ask("The flows folder", "~/o_1/druid-maestro/.maestro/flows"));
      if (report && flows && fs.existsSync(report)) queue.push({ kind: "app", ...common, report, flows });
      else post("App UI skipped: no report at that path.");
    }
    if (!queue.length) return post("Nothing to launch.");
    runQueue(queue, []);
  }

  async function webInputs(o, baseUrl) {
    if (!baseUrl) baseUrl = await ask("The web app's base URL for this environment (e.g. https://jupiter.1mg.com)");
    if (!baseUrl) return null;
    const v = await ctl.json(helper("preflight", ["check", "--feature", o.feature, ...(o.rfcRunDir ? ["--rfc-run-dir", o.rfcRunDir] : []),
      "--engine", engineFor(o.engine) || "claude-code", "--no-network", "--json"]));
    const pw = (v.checks || []).find((c) => c.id === "playwright");
    if (pw && pw.level !== "ok" && pw.fix) post(`Playwright: ${pw.msg} → ${pw.fix}`);
    const login = (v.checks || []).find((c) => c.id === "ui_login") || {};
    let uiLogin = "";
    let uiAuth = "";
    if (login.level === "ok") uiLogin = v.resolved?.ui_login || "";
    else if (login.level === "fail") {
      post(`Web UI skipped — login: ${login.msg}${login.fix ? ` → ${login.fix}` : ""}`);
      return null;
    } else {
      const ACCOUNT = "Test account (number + OTP) — logs itself in every time";
      const c = await pick("How does the web run log in?", [ACCOUNT, "Recorded session"]);
      if (c === ACCOUNT) {
        post("Write the test number and OTP to a file yourself — never paste them here:\n" +
          "  mkdir -p ~/.secrets/playwright && chmod 700 ~/.secrets ~/.secrets/playwright\n" +
          "  printf '{\"phone\": \"<test number>\", \"otp\": \"<otp>\"}\\n' > ~/.secrets/playwright/<env>-login.json && chmod 600 ~/.secrets/playwright/<env>-login.json");
        uiLogin = expand(await ask("Path of that login file, once written"));
        if (!uiLogin || !nonEmptyFile(uiLogin)) return post("Web UI skipped: no login file at that path."), null;
        const r = await ctl.uvRun(helper("preflight", ["setup-web", "--ui-login", uiLogin]));
        post((r.out + r.err).trim() || "stored the login file's path in config.json");
      } else if (c) {
        post(`Record it once (a browser opens; log in, then close it):\n  cd ${project}/tools/ui-runner && npx playwright codegen --save-storage=$HOME/.secrets/playwright/<env>.json ${baseUrl}`);
        uiAuth = expand(await ask("Path of the recorded session file"));
        if (!uiAuth || !nonEmptyFile(uiAuth)) return post("Web UI skipped: no session file at that path."), null;
      } else return null;
    }
    const WATCH = "Watch the browser (visible, slowed down, one test at a time)";
    const headed = (await pick("Watch it run?", [WATCH, "Run in the background"])) === WATCH;
    return { baseUrl, uiLogin, uiAuth, headed };
  }

  function runQueue(queue, done) {
    const next = queue.shift();
    if (!next) return report(done[0]?.runDir, done.map((d) => d.kind));
    const job = launch({
      dir: next.runDir, kind: next.kind, label: `${path.basename(next.runDir)} · ${next.kind}`,
      argv: harnessCmd(next.kind, next), logName: `run.${next.kind}.out`,
      onDone: () => runQueue(queue, [...done, next]),
    });
    if (job) post(`Running ${next.kind} on ${next.env} (build ${next.build}) in the background.`);
  }

  // ---- reports ----

  async function report(dir) {
    if (!dir) return;
    const st = await ctl.statusJson(dir);
    if (st._error) return post(`test: could not read the run's status — ${st._error}`);
    if (st.next?.step === "fix") post(st.next.why?.message || `${st.next.stage} stopped.`);
    const lines = [`Results — ${dir}`, ...resultLines("backend", st.results), ...resultLines("web UI", st.results_web),
      ...resultLines("app UI", st.results_app), ...agentLines("web, by agent", st.results_agent),
      ...agentLines("Android", st.results_android)].map((l) => l.replace("<project>", project));
    if (lines.length === 1) lines.push("No execution results yet.");
    lines.push(...failureHints(st));
    lines.push("FAIL is a defect, BLOCKED is missing data or environment (see qa/prerequisites.md), ERROR is the harness. " +
      "Manual cases, and UI cases left for a person, are in the same Kiwi test run to execute by hand.");
    if (st.duration_s) lines.push(`Whole run: ${[fmtMin(st.duration_s), fmtCost(st.cost_usd)].filter(Boolean).join(", ")}.`);
    post(lines.join("\n"));
  }

  // ---- /test execute (execute.md) ----

  async function execute(argv) {
    const eng = engineFor(argv);
    let args = withoutEngine(argv);
    if (["ui-agent", "android"].includes(args[0])) return executeAgent(args, eng);
    const kind = ["web", "backend", "all"].includes(args[0]) ? args[0] : "web";
    const rd = argValue(args, "--run-dir");
    if (rd) args = args.map((a, i) => (args[i - 1] === "--run-dir" ? path.resolve(latest.cwd, expand(a)) : a));
    if ((kind === "web" || kind === "all") && !args.includes("--only-existing")) {
      const p = await ctl.json(helper("execute", ["--pending", ...(rd ? ["--run-dir", argValue(args, "--run-dir")] : [])]));
      if (p._error) return post(`test execute: ${p._error}`);
      if (p.error) return post(`Kiwi is unreachable: ${p.error}`);
      if (!p.tests.length && !p.new.length && kind === "web") return post("No CONFIRMED web case in Kiwi yet.");
      if (p.new.length && p.tests.length) {
        const NOW = `Run the ${p.tests.length} existing now`;
        const c = await pick(`${p.new.length} newly confirmed web case(s) have no test yet — run the existing tests now, or write theirs first?`,
          [NOW, `Write ${p.new.length} new test(s) first (~1–2 min per case, then everything runs)`]);
        if (c === undefined) return;
        if (c === NOW) args.push("--only-existing");
      } else if (p.new.length) post(`Writing tests for ${p.new.length} newly confirmed case(s) first, then running them.`);
    }
    const engFlag = eng ? ["--engine", eng] : [];
    let printed;
    for (let tries = 0; ; tries++) {
      const r = await ctl.uvRun(helper("execute", [...args, ...engFlag, "--print"]));
      printed = parsePrint(r.out + "\n" + r.err);
      if (!printed.missing.length && !printed.cmds.length) return post(`test execute: ${(r.err || r.out).trim().split("\n").pop()}`);
      if (!printed.missing.length) break;
      if (tries >= 3) return post(`test execute: still missing ${printed.missing.join(", ")}`);
      for (const m of printed.missing) {
        if (m.startsWith("--env")) {
          const envs = await ctl.json(helper("preflight", ["envs", "--json"]));
          const e = Array.isArray(envs) && envs.length
            ? await pickOrType("Which environment?", envs.map((x) => ({ label: `${x.env} — ${x.base_url}`, value: x.env })), "Environment name")
            : await ask("Environment name");
          if (!e) return;
          args.push("--env", e);
        } else if (m.startsWith("--build")) {
          args.push("--build", (await ask("Build (a version or git sha)", "unknown")) || "unknown");
        } else if (m.startsWith("--curl")) {
          const c = expand(await ask("Path of a captured curl against the target host (its contents are never read here)"));
          if (!c || !nonEmptyFile(c)) return post("No readable curl file at that path.");
          args.push("--curl", c);
          const KEEP = "Yes, remember this path";
          if ((await pick("Store the curl's path in config.json for next time?", [KEEP, "No"])) === KEEP) {
            const s = await ctl.uvRun(helper("preflight", ["setup-web", "--curl", c]));
            post((s.out + s.err).trim());
          }
        } else return post(`test execute: missing ${m} — /test --run-dir <run> generates what is missing.`);
      }
    }
    const dir = printed.runDir;
    const shown = [argValue(printed.cmds[0].split(" "), "--env"), argValue(printed.cmds[0].split(" "), "--build")];
    post(`execute ${kind}: ${dir} · env ${shown[0]} · build ${shown[1]}`);
    if ((kind === "web" || kind === "all") && !args.includes("--headed")) {
      const WATCH = "Watch the browser";
      if ((await pick("Watch it run?", [WATCH, "Run in the background"])) === WATCH) args.push("--headed");
    }
    const job = launch({
      dir, kind: `execute-${kind}`, label: `${path.basename(dir)} · execute ${kind}`,
      argv: helper("execute", [...args, ...engFlag]), logName: "run.execute.out",
      onDone: () => report(dir),
    });
    if (job) post(`Running in the background; progress shows above the editor. /test attach ${dir} follows it from another session.`);
  }

  // ---- /test execute ui-agent | android (execute.md §2a, §3) ----

  let saidAgentCost = false;

  /** An agent executes each CONFIRMED UI case. Every question execute.md has Claude ask is a dialog here. */
  async function executeAgent(args, eng) {
    const kind = args[0];
    const has = (f) => args.includes(f);
    const rd = argValue(args, "--run-dir");
    if (rd) args = args.map((a, i) => (args[i - 1] === "--run-dir" ? path.resolve(latest.cwd, expand(a)) : a));
    if (kind === "ui-agent" && !has("--platform")) args.push("--platform", "auto"); // each case where its tags say
    // what --pending reads: where the cases are, and what decides how each one runs
    const sel = () => ["--run-dir", "--feature", "--kiwi-plan", "--platform", "--android-mode", "--maestro-repo", "--untagged"]
      .flatMap((f) => (argValue(args, f) ? [f, argValue(args, f)] : [])).concat(has("--include-manual") ? ["--include-manual"] : []);
    let p;
    let askedManual = false;
    for (;;) {
      p = await ctl.json(helper("execute", [kind, "--pending", ...sel()]));
      if (p._error) return post(`test execute ${kind}: ${p._error}`);
      if (p.error) return post(`Kiwi is unreachable, or the plan does not exist: ${p.error}`);
      const a = p.android;
      if (a?.error && /not set up/i.test(a.error)) {
        post(a.error);
        if (kind === "android" || !(p.will_run || []).length) return;
        const WEB = "Run the web cases only";
        if ((await pick("Android isn't set up on this machine.", [WEB, "Stop"])) !== WEB) return;
        args[args.indexOf("--platform") + 1] = "web";
        continue;
      }
      if (a?.error) post(`android: ${a.error}`);
      const manual = [...(p.manual_left || []), ...(a?.manual_left || [])];
      if (manual.length && !has("--include-manual") && !askedManual) {
        askedManual = true;
        const INC = "Include them";
        const c = await pick(`${manual.length} CONFIRMED manual case(s) have a surface tag and an agent could run them (${manual.slice(0, 8).join(", ")}).`,
          [INC, "Leave them for a person"]);
        if (c === undefined) return;
        if (c === INC) {
          args.push("--include-manual");
          continue;
        }
      }
      if (a?.untagged?.length && !has("--untagged")) {
        const where = { Web: "web", Android: "android", Both: "both", "Skip them": "skip" };
        const c = await pick(`${a.untagged.length} case(s) have no platform tag — where should they run? (Tag them surface:web / surface:android in Kiwi and this question goes away.)`,
          Object.keys(where));
        if (c === undefined) return;
        args.push("--untagged", where[c]);
        continue;
      }
      break;
    }
    const sum = pendingSummary(p);
    if (!sum.web && !sum.flow && !sum.agent) {
      return post([`test execute ${kind}: no CONFIRMED UI case with an expected result to run.`, ...sum.lines].join("\n"));
    }
    post(sum.lines.join("\n"));

    const TEST = "Yes, a test account";
    if ((p.needs_session || []).length && !has("--ui-auth")) {
      const ADD = "Add a recorded session file";
      const c = await pick(`${p.needs_session.length} case(s) need a logged-in user. Without a recorded session (--ui-auth, or testing.ui_auth in config) they come back BLOCKED; the agent never logs in with a number and OTP.`,
        [ADD, "Run with what config has"]);
      if (c === undefined) return;
      if (c === ADD) {
        const f = expand(await ask("Path of the Playwright storage-state file (its contents are never read here)"));
        if (!f || !nonEmptyFile(f)) return post("No session file at that path.");
        args.push("--ui-auth", f);
      }
    }
    const groups = Object.keys(p.needs_allow || {});
    if (groups.length && !has("--allow")) {
      const what = groups.map((g) => (g === "coupons" ? "apply coupons" : "redeem or toggle NeuCoins")).join(" and ");
      const YES = "Yes, allow";
      const c = await pick(`Some cases ${what}, which the guard refuses by default (they come back BLOCKED). Allow it for this run? The agent is told to undo what it applied.`,
        [YES, "No"]);
      if (c === undefined) return;
      if (c === YES) args.push("--allow", groups.join(","));
    }
    const allowed = (argValue(args, "--allow") || "").split(",");
    const androidRuns = sum.flow + sum.agent > 0;
    if ((has("--production") || allowed.includes("neucoins")) && !has("--test-account")) {
      const GUEST = "It runs as a guest (no session)";
      const options = androidRuns || allowed.includes("neucoins") ? [TEST, "Stop"] : [TEST, GUEST, "Stop"];
      const c = await pick(`${has("--production") ? "This run is on PRODUCTION. " : ""}The agent adds to the cart and changes what the account sees. ` +
        `Is the account (${androidRuns ? "the device's, and any recorded browser session" : "the recorded browser session"}) a TEST account, not a customer's?`, options);
      if (c === TEST) args.push("--test-account");
      else if (c !== GUEST) return post("Not confirmed as a test account — nothing was run.");
    }
    if (!saidAgentCost) {
      saidAgentCost = true;
      post("Each case an agent runs is a model session on every run (cost and time are per case), and it acts on the environment as a real user. " +
        "An Android case with a validated flow costs nothing and runs the same every time. Axon keeps a backup of every agent session, page contents included.");
    }

    const engFlag = eng ? ["--engine", eng] : [];
    let printed;
    for (let tries = 0; ; tries++) {
      const r = await ctl.uvRun(helper("execute", [...args, ...engFlag, "--print"]));
      printed = parsePrint(r.out + "\n" + r.err);
      if (!printed.missing.length && !printed.cmds.length) return post(`test execute ${kind}: ${(r.err || r.out).trim().split("\n").pop()}`);
      if (!printed.missing.length) break;
      if (tries >= 3) return post(`test execute ${kind}: still missing ${printed.missing.join(", ")}`);
      for (const m of printed.missing) {
        if (m.startsWith("--env")) {
          const envs = await ctl.json(helper("preflight", ["envs", "--json"]));
          const e = Array.isArray(envs) && envs.length
            ? await pickOrType("Which environment?", envs.map((x) => ({ label: `${x.env} — ${x.base_url}`, value: x.env })), "Environment name")
            : await ask("Environment name");
          if (!e) return;
          args.push("--env", e);
        } else if (m.startsWith("--build")) {
          args.push("--build", (await ask("Build (a version or git sha)", "unknown")) || "unknown");
        } else return post(`test execute ${kind}: missing ${m}`);
      }
    }
    const dir = printed.runDir;
    post(`execute ${kind}: ${dir} · ${printed.cmds.length} launch(es) · agents on ${eng || "the harness's engine"}`);
    if (sum.web && !has("--headed")) {
      const WATCH = "Watch the browser (one case at a time)";
      if ((await pick("Watch it run?", [WATCH, "Run in the background"])) === WATCH) args.push("--headed");
    }
    const job = launch({
      dir, kind: `execute-${kind}`, label: `${path.basename(dir)} · execute ${kind}`,
      argv: helper("execute", [...args, ...engFlag]), logName: "run.execute.out",
      onDone: () => report(dir),
    });
    if (job) post(`Running in the background; progress shows above the editor. /test attach ${dir} follows it from another session.`);
  }

  // ---- /test evaluate (evaluate.md) ----

  async function evaluate(argv) {
    const eng = engineFor(argv);
    let args = withoutEngine(argv);
    if (argValue(args, "--run-dir")) args = args.map((a, i) => (args[i - 1] === "--run-dir" ? path.resolve(latest.cwd, expand(a)) : a));
    const engFlag = eng ? ["--engine", eng] : [];
    const r = await ctl.uvRun(helper("evaluation", [...args, ...engFlag, "--print"]));
    const text = (r.out + "\n" + r.err).trim();
    if (/missing /.test(text) || r.code !== 0) return post(`test evaluate: ${text.split("\n").pop()}\nA suite that was never generated needs /test.`);
    const dir = parsePrint(text).runDir;
    const st = await ctl.statusJson(dir);
    post(`Evaluating ${dir} — ${st.feature || "?"}, ${st.cases?.total ?? "?"} cases. About 15 min and $3–4.`);
    const job = launch({
      dir, kind: "evaluate", label: `${path.basename(dir)} · evaluate`,
      argv: helper("evaluation", [...args, ...engFlag]), logName: "run.evaluate.out",
      onDone: async () => {
        const s = await ctl.statusJson(dir);
        if (s.eval) return post(evalText(s, readJson(path.join(dir, "qa", "eval.json"))));
        post(s.next?.why?.message || `evaluate stopped: ${JSON.stringify(s.stages?.evaluate?.failures || []).slice(0, 400)}`);
        const c = await pick("Evaluate stopped — retry?", ["Retry", "Later"]);
        if (c === "Retry") return evaluate(argv);
      },
    });
    if (job) post("Progress shows above the editor.");
  }

  // ---- test_status: read-only, for "how is the run going?" ----

  async function snapshot(dir) {
    const logs = path.join(dir, "logs");
    if (!fs.existsSync(logs)) throw new Error(`${dir} is not a testing run folder (no logs/)`);
    const parts = [`run: ${dir}`];
    const pidInfo = readJson(path.join(logs, PID_FILE));
    if (pidInfo.pid) parts.push(`launch: ${pidInfo.kind} — ${pidAlive(pidInfo.pid) ? `running (pid ${pidInfo.pid})` : "not running"}; log logs/${pidInfo.log}`);
    const now = await ctl.uvRun(helper("progress", [dir, "--now"]));
    if (now.out.trim()) parts.push(`now: ${now.out.trim()}`);
    const st = await ctl.uvRun(helper("status", [dir, "--json"]));
    if (st.out.trim()) parts.push(`status --json:\n${st.out.trim()}`);
    const act = tail(path.join(logs, "activity.log"), 15);
    if (act?.length) parts.push(`activity.log (last ${act.length} lines):\n${act.join("\n")}`);
    return parts.join("\n\n");
  }

  pi.registerTool({
    name: "test_status",
    label: "testing run status",
    description: "State of a harness testing run (test-agent): whether a launch is running, what the current stage is doing, " +
      "status --json (stages, cases, Kiwi, results, next step) and the tail of logs/activity.log. Read-only.",
    promptGuidelines: [
      "For questions about a testing run (status, progress, results, cost), call test_status instead of running shell commands; " +
        "use read, grep, find and ls for anything else in the run folder.",
      "Never read, print or copy login files, curls, ~/.tcms.conf or session files: they hold credentials.",
    ],
    parameters: {
      type: "object",
      properties: { run_dir: { type: "string", description: "The testing run folder. Omit for the run /test is following in this session." } },
    },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
    async execute(_id, params, _signal, _onUpdate, ctx) {
      const dir = params?.run_dir ? path.resolve(ctx?.cwd || process.cwd(), expand(params.run_dir)) : [...jobs.keys()].pop() || lastDir;
      if (!dir) throw new Error("no testing run is being followed in this session; pass run_dir");
      return { content: [{ type: "text", text: await snapshot(dir) }], details: undefined };
    },
  });

  pi.on("session_start", (_e, ctx) => {
    latest = ctx;
  });
  pi.on("session_shutdown", () => {
    if (timer) clearInterval(timer);
    timer = null;
    for (const job of jobs.values()) {
      job.stopWatcher();
      job.child?.unref();
    }
    jobs.clear();
  });

  pi.registerCommand("test", {
    description: "test-agent: QA cases from an RFC run → Kiwi review → execute (backend, web UI, app UI); execute / evaluate / status",
    getArgumentCompletions: (prefix) =>
      ["execute ", "evaluate", "status ", "attach ", "doctor", "--feature ", "--rfc-run-dir ", "--run-dir ", "--test-data ",
        "--contract ", "--plan ", "--figma ", "--engine "]
        .filter((c) => c.startsWith(prefix))
        .map((c) => ({ value: c, label: c.trim() })),
    handler: async (args, ctx) => {
      latest = ctx;
      if (!project || !fs.existsSync(path.join(project, "pyproject.toml"))) {
        ctx.ui.notify(`test: the project is missing at ${project}; rerun python -m rfc_agent.axon_agent install`, "error");
        return;
      }
      const { sub, argv } = parseTestArgs(splitArgs(args));
      try {
        if (sub === "help") return post(HELP);
        if (sub === "doctor") {
          ctx.ui.setWorkingMessage?.("test: checking Axon…");
          const r = await ctl.uvRun(["python", "-m", "rfc_agent.axon_agent", "doctor", "--no-atlassian"]);
          ctx.ui.setWorkingMessage?.();
          return post((r.out + r.err).trim());
        }
        if (sub === "status") {
          const dir = argv[0] ? path.resolve(ctx.cwd, expand(argv[0])) : [...jobs.keys()].pop() || lastDir;
          if (!dir) return post("test status: which run? /test status <run dir>");
          const st = await ctl.statusJson(dir, argv.includes("--kiwi"));
          if (st._error) return post(`test status: ${st._error}`);
          const stages = Object.entries(st.stages || {}).map(([k, v]) => `  ${v.gate_passed ? "✓" : v.status === "ok" ? "•" : "✗"} ${k}${v.duration_s ? ` (${fmtMin(v.duration_s)})` : ""}`);
          return post([`${dir} — ${st.feature}`, ...stages, `next: ${st.next?.step}${st.next?.stage ? ` (${st.next.stage})` : ""}`,
            ...(st.kiwi ? kiwiStatusLines(st.kiwi) : []), ...resultLines("backend", st.results), ...resultLines("web UI", st.results_web),
            ...resultLines("app UI", st.results_app)].join("\n").replaceAll("<project>", project));
        }
        if (sub === "attach") {
          const dir = path.resolve(ctx.cwd, expand(argv[0] || ""));
          if (!argv[0] || !fs.existsSync(path.join(dir, "logs"))) return ctx.ui.notify("test: /test attach <run dir> — that folder has no logs/", "error");
          if (jobs.has(dir)) return ctx.ui.notify(`test: already following ${dir}`, "info");
          const info = readJson(path.join(dir, "logs", PID_FILE));
          if (!info.pid || !pidAlive(info.pid)) {
            post(`Nothing is running in ${path.basename(dir)}; here is where it stands.`);
            return resume(dir, engineArgs(argv));
          }
          let from = 0;
          try {
            from = fs.statSync(path.join(dir, "logs", "activity.log")).size;
          } catch {}
          const o = { ...runInfo(dir), engine: engineArgs(argv), overrides: [] };
          const after = info.kind === "generate" || info.kind === "store" ? () => afterPhaseA(o)
            : info.kind === "evaluate" ? () => evaluateDone(dir) : () => report(dir);
          jobs.set(dir, new Job({ dir, kind: info.kind, label: `${path.basename(dir)} · ${info.kind}`, pid: info.pid, from, ctl, onDone: after }));
          lastDir = dir;
          if (!timer) timer = setInterval(tick, POLL_MS);
          return ctx.ui.notify(`test: following ${dir} (${info.kind})`, "info");
        }
        if (sub === "execute") return await execute(argv);
        if (sub === "evaluate") return await evaluate(argv);
        return await startOrResume(argv);
      } catch (e) {
        post(`test: ${e.message}`);
      }
    },
  });

  async function evaluateDone(dir) {
    const s = await ctl.statusJson(dir);
    post(s.eval ? evalText(s, readJson(path.join(dir, "qa", "eval.json"))) : s.next?.why?.message || "evaluate stopped.");
  }
}
