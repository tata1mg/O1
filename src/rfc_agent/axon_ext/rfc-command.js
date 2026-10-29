/**
 * rfc-command — `/rfc` inside an Axon (Pi) session: the same pipeline `/rfc-agent:rfc` runs from Claude Code.
 *
 * The pipeline itself is unchanged: this starts `uv run rfc … --confirm-file` as a detached child, exactly as
 * the Claude Code command does, and is its progress view and pause UI. Nothing here talks to a model.
 *
 *   progress   run.out ([rfc] banners, done/STOPPED lines) and logs/progress.jsonl (`text` milestones) feed a
 *              widget above the editor and a status-bar entry; logs/activity.log counts started agents.
 *   pauses     a `WAITING FOR …` line names a proposal file and the answers file the CLI waits for. The
 *              proposal is asked with Pi dialogs (select / input) and the answers are written atomically, in
 *              the formats commands/scope.md §4, investigate.md §3 and review.md §3 define.
 *   finish     the CLI's own closing lines (time, cost, unresolved findings, output paths) are posted into the
 *              conversation, so the user can go on to ask the model about the RFC.
 *
 * The child is detached and writes to files, so it outlives the Axon session; `/rfc attach <run dir>` picks a
 * running (or finished) run up again, including a pause that is still waiting.
 *
 * Installed by `python -m rfc_agent.axon_agent install`, which writes a two-line stub into Axon's extensions
 * dir that imports this file and passes the project root. No dependencies: Pi loads it with jiti, and the pure
 * helpers below are exported so plain node can test them (tests/fixtures/axon/rfc_command_cases.mjs).
 */

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const POLL_MS = 1000;
const WIDGET = "rfc-agent";
const MAX_LINES = 6;

// ---- pure helpers (tested under node) -----------------------------------------------------------------

/** Split a command line the way a shell would for plain words and '…' / "…" quoting. */
export function splitArgs(s) {
  const out = [];
  let cur = "";
  let quote = null;
  let has = false;
  for (const ch of s || "") {
    if (quote) {
      if (ch === quote) quote = null;
      else cur += ch;
    } else if (ch === "'" || ch === '"') {
      quote = ch;
      has = true;
    } else if (/\s/.test(ch)) {
      if (has || cur) out.push(cur);
      cur = "";
      has = false;
    } else {
      cur += ch;
    }
  }
  if (has || cur) out.push(cur);
  return out;
}

/** The value of `--flag v` or `--flag=v`, or undefined. */
export function argValue(argv, flag) {
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === flag) return argv[i + 1];
    if (argv[i].startsWith(flag + "=")) return argv[i].slice(flag.length + 1);
  }
  return undefined;
}

/** `/rfc` inside Axon means the agents run on Axon too, unless the user picked engines themselves. */
export function withEngine(argv) {
  const picked = argv.some((a) => /^--(stage-)?engine(=|$)/.test(a));
  return picked ? argv : [...argv, "--engine", "axon"];
}

/** Whether any stage of this invocation runs on Axon (then the Axon doctor is the preflight). */
export function usesAxon(argv) {
  const e = argValue(argv, "--engine");
  if (e === "axon") return true;
  return argv.some((a, i) => (a === "--stage-engine" && /=axon$/.test(argv[i + 1] || "")) || /^--stage-engine=.*=axon$/.test(a));
}

const WAITING = /WAITING FOR (SELECTION|PAUSE (\d))[^:]*: proposal at (.+?) — answers expected at (.+?)\s*$/;

/** A `WAITING FOR …` line → {kind: "scope"|"investigate"|"triage", n, proposal, answers}, else null. */
export function parseWaiting(line) {
  const m = WAITING.exec(line);
  if (!m) return null;
  const n = m[2] ? Number(m[2]) : 0;
  const kind = !m[2] ? "scope" : n === 3 ? "triage" : "investigate";
  return { kind, n, proposal: m[3], answers: m[4] };
}

const RELAY = [
  /\[rfc\] ── (.+?) ──/, // stage banner
  /\[rfc\] (done in .+)/,
  /\[rfc\] (STOPPED at .+)/,
  /\[rfc\] (read: .+)/,
  /\[rfc\] (full: .+)/,
  /(press ⌥⌘P.*)/,
  /(ONE-TIME SETUP.*)/,
  /(Figma plugin: import it from the repo.*)/,
  /(an OLD rfc-agent plugin window is still exporting.*)/,
  /(Langfuse observability is ON but the Langfuse keys are missing.*)/,
];

/** A run.out line worth showing → its text, else null. */
export function relayLine(line) {
  for (const re of RELAY) {
    const m = re.exec(line);
    if (m) return m[1].trim();
  }
  return null;
}

/** A logs/progress.jsonl line → its milestone text, else null. */
export function progressText(line) {
  try {
    const t = JSON.parse(line).text;
    return typeof t === "string" && t.trim() ? t.trim() : null;
  } catch {
    return null;
  }
}

/** Option label for a checklist row. */
export function checkLabel(on, label, note) {
  return `${on ? "[x]" : "[ ]"} ${label}${note ? "  — " + note : ""}`;
}

/** "a, b=feature/x , c" → {add_repos: ["a","c"], branches: {b: "feature/x"}}. */
export function parseRepoExtras(text) {
  const add_repos = [];
  const branches = {};
  for (const part of (text || "").split(",").map((s) => s.trim()).filter(Boolean)) {
    const eq = part.indexOf("=");
    if (eq > 0) branches[part.slice(0, eq).trim()] = part.slice(eq + 1).trim();
    else add_repos.push(part);
  }
  return { add_repos, branches };
}

/** "R-F1.2: disagree; R-X.1: ok" → {"R-F1.2": "disagree", "R-X.1": "ok"}. */
export function parseNotes(text) {
  const notes = {};
  for (const part of (text || "").split(/[;\n]/).map((s) => s.trim()).filter(Boolean)) {
    const c = part.indexOf(":");
    if (c > 0 && part.slice(c + 1).trim()) notes[part.slice(0, c).trim()] = part.slice(c + 1).trim();
  }
  return notes;
}

/** Write JSON so the CLI (which polls for the file) never reads half of it. */
export function writeAnswers(file, data) {
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
  fs.renameSync(tmp, file);
}

/** New complete lines of a growing file since `state.offset` (state is mutated). */
export function readNewLines(file, state) {
  let size;
  try {
    size = fs.statSync(file).size;
  } catch {
    return [];
  }
  if (size < state.offset) state.offset = 0; // truncated or replaced: start over
  if (size === state.offset) return [];
  const fd = fs.openSync(file, "r");
  try {
    const buf = Buffer.alloc(size - state.offset);
    fs.readSync(fd, buf, 0, buf.length, state.offset);
    const text = (state.rest || "") + buf.toString("utf8");
    state.offset = size;
    const lines = text.split("\n");
    state.rest = lines.pop();
    return lines;
  } finally {
    fs.closeSync(fd);
  }
}

// ---- dialogs ------------------------------------------------------------------------------------------

const DONE = "✔ Done";

/** Toggle list on top of ui.select (Pi has no multi-select dialog). Escape keeps the current ticks. */
async function checklist(ui, title, rows) {
  const on = rows.map((r) => !!r.on);
  for (;;) {
    const opts = rows.map((r, i) => checkLabel(on[i], r.label, r.note));
    const pick = await ui.select(`${title}\n(pick a row to toggle, then ${DONE})`, [DONE, ...opts]);
    if (pick === undefined || pick === DONE) return on;
    const i = opts.indexOf(pick);
    if (i >= 0) on[i] = !on[i];
  }
}

async function askScope(ui, prop) {
  const items = prop.items || [];
  const keep = new Set(items.filter((it) => it.include).map((it) => it.n));
  const ofKind = (k) => items.filter((it) => it.kind === k);
  const header = String(prop.header || "").split("\n")[0];
  let extras = { add_repos: [], branches: {} };

  for (;;) {
    const repos = ofKind("repo");
    if (repos.length) {
      const proposed = repos.filter((r) => keep.has(r.n)).map((r) => r.label).join(", ") || "none";
      const on = await checklist(ui, `${header}\nWhich repos are in scope? Proposed: ${proposed}`,
        repos.map((r) => ({ label: r.label, note: r.note, on: keep.has(r.n) })));
      repos.forEach((r, i) => (on[i] ? keep.add(r.n) : keep.delete(r.n)));
    }
    const extra = await ui.input("Add repos or override a branch (comma-separated: name, or repo=branch). Empty for none.", "");
    extras = parseRepoExtras(extra);
    if (repos.some((r) => keep.has(r.n)) || extras.add_repos.length) break;
    ui.notify("At least one repo must be in scope.", "warning");
  }

  for (const [kind, what] of [["page", "Confluence pages"], ["jira", "Jira issues"]]) {
    const rows = ofKind(kind);
    if (!rows.length) continue;
    const on = await checklist(ui, `${what} — NOT included unless ticked`,
      rows.map((r) => ({ label: r.label, note: r.note, on: keep.has(r.n) })));
    rows.forEach((r, i) => (on[i] ? keep.add(r.n) : keep.delete(r.n)));
  }

  const figma = ofKind("figma");
  if (figma.length) {
    const proposed = figma.filter((f) => keep.has(f.n));
    let mode = "pick";
    if (figma.length > 8) {
      const KEEP = `Keep the proposed ${proposed.length} sections (${proposed.map((f) => f.label).join(", ")})`;
      const PICK = "Pick sections myself";
      const SKIP = "Skip Figma";
      const c = await ui.select("Figma sections", [KEEP, PICK, SKIP]);
      mode = c === PICK ? "pick" : c === SKIP ? "skip" : "keep";
    }
    if (mode === "skip") figma.forEach((f) => keep.delete(f.n));
    if (mode === "pick") {
      const on = await checklist(ui, "Figma sections", figma.map((f) => ({ label: f.label, note: f.note, on: keep.has(f.n) })));
      figma.forEach((f, i) => (on[i] ? keep.add(f.n) : keep.delete(f.n)));
    }
  }
  return { selected: [...keep].sort((a, b) => a - b), ...extras };
}

async function askInvestigate(ui, prop, n) {
  const items = prop.items || [];
  const selected = new Set();
  let feedback = "";
  const answers = {};

  const retries = items.filter((it) => it.kind === "retry");
  if (retries.length) {
    const on = await checklist(ui, "Retry these repo snapshots? (unticked = their hops stay untraced)",
      retries.map((r) => ({ label: r.label, note: r.note, on: r.include })));
    retries.forEach((r, i) => on[i] && selected.add(r.n));
  }
  if (n === 1) {
    const ok = "Plan looks right";
    const c = await ui.select(`${String(prop.header || "").trim()}\n\nThe tracer plan:`, [ok, "Change it"]);
    if (c && c !== ok) feedback = (await ui.input("What should change in the plan?", "")) || "";
  }
  for (const q of items.filter((it) => it.kind === "question")) {
    const spec = q.question || {};
    const LEAVE = "Leave open";
    const OWN = "Type my own answer";
    const opts = [...(spec.suggested_answer ? [spec.suggested_answer] : []), LEAVE, OWN];
    const c = await ui.select(`${q.key}: ${spec.question || q.label}${spec.blocks ? `\n(blocks: ${spec.blocks})` : ""}`, opts);
    const text = c === OWN ? (await ui.input(`${q.key}: your answer`, "")) || "" : c && c !== LEAVE ? c : "";
    if (text.trim()) {
      selected.add(q.n);
      answers[q.key] = text.trim();
    }
  }
  return { selected: [...selected].sort((a, b) => a - b), feedback, answers };
}

async function askTriage(ui, prop) {
  const items = (prop.items || []).filter((it) => it.kind === "finding");
  const on = await checklist(ui, `${String(prop.header || "").split("\n")[0]}\nFindings to FIX (BLOCKER/MAJOR pre-ticked)`,
    items.map((f) => ({ label: f.key, note: f.label, on: f.include })));
  const notes = parseNotes(await ui.input("Notes on findings (optional): R-F1.2: why; R-X.1: why", ""));
  return { selected: items.filter((_, i) => on[i]).map((f) => f.n), notes, feedback: "" };
}

// ---- one watched run ----------------------------------------------------------------------------------

class Run {
  constructor(runDir, { pi, getCtx, child, pid }) {
    this.dir = runDir;
    this.name = path.basename(runDir);
    this.pi = pi;
    this.getCtx = getCtx;
    this.child = child;
    this.pid = pid;
    this.exited = false;
    this.exitCode = null;
    this.lines = [];
    this.stage = "starting";
    this.agents = 0;
    this.closing = [];
    this.files = { out: { offset: 0 }, progress: { offset: 0 }, activity: { offset: 0 } };
    this.pending = new Map(); // answers path -> waiting info
    this.asking = false;
    this.t0 = Date.now();
    if (child) {
      child.on("exit", (code) => {
        this.exited = true;
        this.exitCode = code;
      });
    }
  }

  get ui() {
    const ctx = this.getCtx();
    return ctx && ctx.hasUI ? ctx.ui : null;
  }

  alive() {
    if (this.child) return !this.exited;
    try {
      process.kill(this.pid, 0);
      return true;
    } catch {
      return false;
    }
  }

  push(text) {
    this.lines.push(text);
    if (this.lines.length > MAX_LINES) this.lines.shift();
  }

  render() {
    const ui = this.ui;
    if (!ui) return;
    const mins = Math.floor((Date.now() - this.t0) / 60000);
    const head = `rfc · ${this.name} · ${this.stage} · ${this.agents} agent(s) started · ${mins} min`;
    ui.setWidget(WIDGET, [head, ...this.lines.map((l) => "  " + l)]);
    ui.setStatus(WIDGET, `rfc: ${this.stage}`);
  }

  poll() {
    const logs = path.join(this.dir, "logs");
    let changed = false;
    for (const line of readNewLines(path.join(logs, "run.out"), this.files.out)) {
      const w = parseWaiting(line);
      if (w) {
        if (!fs.existsSync(w.answers)) this.pending.set(w.answers, w);
        continue;
      }
      const t = relayLine(line);
      if (!t) continue;
      changed = true;
      const banner = /\[rfc\] ── (.+?) ──/.exec(line);
      if (banner) this.stage = banner[1].split(" — ")[0];
      if (/^(done in|STOPPED at|read:|full:)/.test(t)) this.closing.push(t);
      else if (/^(press ⌥⌘P|ONE-TIME SETUP|Figma plugin|an OLD rfc-agent|Langfuse)/.test(t)) this.ui?.notify(t, "warning");
      this.push(banner ? `── ${banner[1]}` : t);
    }
    for (const line of readNewLines(path.join(logs, "progress.jsonl"), this.files.progress)) {
      const t = progressText(line);
      if (t) {
        this.push(t);
        changed = true;
      }
    }
    for (const line of readNewLines(path.join(logs, "activity.log"), this.files.activity)) {
      if (line.includes("▶ started")) {
        this.agents++;
        changed = true;
      }
    }
    for (const ans of this.pending.keys()) if (fs.existsSync(ans)) this.pending.delete(ans);
    if (changed) this.render();
    const alive = this.alive();
    // A proposal left by a process that is gone has nobody waiting for its answers.
    if (alive && this.pending.size && !this.asking) this.ask([...this.pending.values()][0]);
    return alive;
  }

  async ask(w) {
    const ui = this.ui;
    if (!ui) return;
    this.asking = true;
    try {
      const prop = JSON.parse(fs.readFileSync(w.proposal, "utf8"));
      ui.notify(`rfc ${this.name}: ${w.kind === "scope" ? "Scope selection" : `pause ${w.n}`} is waiting for you`, "info");
      const data = w.kind === "scope" ? await askScope(ui, prop)
        : w.kind === "triage" ? await askTriage(ui, prop)
        : await askInvestigate(ui, prop, w.n);
      if (!fs.existsSync(w.answers)) writeAnswers(w.answers, data);
      this.pending.delete(w.answers);
      this.push(`answered ${w.kind === "scope" ? "the Scope selection" : `pause ${w.n}`}`);
      this.render();
    } catch (e) {
      ui.notify(`rfc: could not ask the pause (${e.message}); answer it with /rfc attach ${this.dir}`, "error");
    } finally {
      this.asking = false;
    }
  }

  finish() {
    const ui = this.ui;
    const ok = this.exitCode === 0 || this.closing.some((l) => l.startsWith("done in"));
    const body = this.closing.length
      ? this.closing.join("\n")
      : `the process ended${this.exitCode != null ? ` with exit ${this.exitCode}` : ""} — see ${path.join(this.dir, "logs", "run.out")}`;
    ui?.setWidget(WIDGET, undefined);
    ui?.setStatus(WIDGET, undefined);
    ui?.notify(`rfc ${this.name}: ${ok ? "done" : "stopped"}`, ok ? "info" : "error");
    const tail = ok
      ? "Read out/rfc.readable.md first; out/rfc.md is the full RFC with citations."
      : "Rerunning the same /rfc command resumes; finished stages are kept.";
    this.pi.sendMessage({
      customType: "rfc-agent",
      content: `rfc-agent run ${this.dir}\n${body}\n${tail}`,
      display: true,
    });
  }
}

// ---- the extension ------------------------------------------------------------------------------------

function runCapture(cmd, args, opts) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { ...opts, stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    let err = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => (err += d));
    p.on("error", (e) => resolve({ code: 127, out, err: err + e.message }));
    p.on("exit", (code) => resolve({ code, out, err }));
  });
}

/** The parent Pi's runtime variables must not leak into the pipeline's own `axon pi` children. */
function childEnv() {
  const env = { ...process.env };
  for (const k of Object.keys(env)) if (k.startsWith("PI_")) delete env[k];
  return env;
}

const HELP = [
  "/rfc --prd <confluence url | page id | file.md> [flags]   run the pipeline (agents on Axon by default)",
  "/rfc --run-dir <run> --from <stage> [flags]               redo a stage onward",
  "/rfc attach <run dir>                                     follow a run started elsewhere (or answer its pause)",
  "/rfc doctor                                               check Axon, models and fleetlens",
  "Flags are the rfc CLI's: --jira, --repos, --no-figma, --preset, --ask, --ask-review, --yes, --langfuse, --engine, --stage-engine.",
].join("\n");

export default function make(pi, { project, uv = "uv" } = {}) {
  let latest = null; // the newest context: dialogs must use the live session's UI
  const runs = new Map();
  let timer = null;

  const tick = () => {
    for (const [dir, run] of runs) {
      let keep = true;
      try {
        keep = run.poll();
      } catch (e) {
        latest?.ui?.notify(`rfc: ${e.message}`, "error");
      }
      if (!keep) {
        runs.delete(dir);
        run.finish();
      }
    }
    if (!runs.size && timer) {
      clearInterval(timer);
      timer = null;
    }
  };
  const watch = (run) => {
    runs.set(run.dir, run);
    run.render();
    if (!timer) timer = setInterval(tick, POLL_MS);
  };
  const uvRun = (args, cwd) => runCapture(uv, ["run", "--project", project, ...args], { cwd, env: childEnv() });

  pi.on("session_start", (_e, ctx) => {
    latest = ctx;
  });
  pi.on("session_shutdown", () => {
    if (timer) clearInterval(timer);
    timer = null;
    for (const run of runs.values()) run.child?.unref();
    runs.clear();
  });

  pi.registerCommand("rfc", {
    description: "rfc-agent: generate a tech RFC from a PRD (Scope → Summarize), agents on Axon",
    getArgumentCompletions: (prefix) =>
      ["--prd ", "attach ", "doctor", "--run-dir ", "--from ", "--engine ", "--stage-engine ", "--ask", "--ask-review", "--yes"]
        .filter((c) => c.startsWith(prefix))
        .map((c) => ({ value: c, label: c.trim() })),
    handler: async (args, ctx) => {
      latest = ctx;
      const ui = ctx.ui;
      const argv = splitArgs(args);
      if (!project || !fs.existsSync(path.join(project, "pyproject.toml"))) {
        ui.notify(`rfc: the rfc-agent project is missing at ${project}; rerun python -m rfc_agent.axon_agent install`, "error");
        return;
      }
      if (!argv.length || argv[0] === "help" || argv[0] === "--help") {
        pi.sendMessage({ customType: "rfc-agent", content: HELP, display: true });
        return;
      }

      if (argv[0] === "doctor") {
        ui.setWorkingMessage?.("rfc: checking Axon…");
        const r = await uvRun(["python", "-m", "rfc_agent.axon_agent", "doctor"], ctx.cwd);
        ui.setWorkingMessage?.();
        pi.sendMessage({ customType: "rfc-agent", content: (r.out + r.err).trim(), display: true });
        return;
      }

      if (argv[0] === "attach") {
        const dir = path.resolve(ctx.cwd, argv[1] || "");
        if (!argv[1] || !fs.existsSync(path.join(dir, "logs"))) {
          ui.notify("rfc: /rfc attach <run dir> — that folder has no logs/", "error");
          return;
        }
        if (runs.has(dir)) return ui.notify(`rfc: already following ${dir}`, "info");
        let pid = 0;
        try {
          pid = Number(fs.readFileSync(path.join(dir, "logs", "axon-rfc.pid"), "utf8"));
        } catch {}
        watch(new Run(dir, { pi, getCtx: () => latest, pid }));
        ui.notify(`rfc: following ${dir}`, "info");
        return;
      }

      const full = withEngine(argv);
      if (usesAxon(full)) {
        ui.setWorkingMessage?.("rfc: checking Axon…");
        const d = await uvRun(["python", "-m", "rfc_agent.axon_agent", "doctor"], ctx.cwd);
        ui.setWorkingMessage?.();
        if (d.code !== 0) {
          pi.sendMessage({ customType: "rfc-agent", content: `rfc: not started — Axon preflight failed\n${(d.out + d.err).trim()}`, display: true });
          return;
        }
      }

      let runDir = argValue(full, "--run-dir");
      if (runDir) runDir = path.resolve(ctx.cwd, runDir);
      else {
        const r = await uvRun(["python", "-m", "rfc_agent.scope", "run-dir", ...full], ctx.cwd);
        runDir = r.out.trim().split("\n").pop();
        if (r.code !== 0 || !runDir) {
          ui.notify(`rfc: could not pick a run folder: ${(r.err || r.out).trim().split("\n").pop()}`, "error");
          return;
        }
      }
      if (runs.has(runDir)) return ui.notify(`rfc: ${runDir} is already running here`, "warning");

      const logs = path.join(runDir, "logs");
      fs.mkdirSync(logs, { recursive: true });
      const outFile = path.join(logs, "run.out");
      const start = fs.existsSync(outFile) ? fs.statSync(outFile).size : 0;
      const fd = fs.openSync(outFile, "a");
      const cli = [...full.filter((a, i) => !(a === "--run-dir" || full[i - 1] === "--run-dir" || a.startsWith("--run-dir="))),
        "--run-dir", runDir, "--confirm-file"];
      const child = spawn(uv, ["run", "--project", project, "rfc", ...cli], {
        cwd: ctx.cwd,
        env: childEnv(),
        detached: true,
        stdio: ["ignore", fd, fd],
      });
      fs.closeSync(fd);
      fs.writeFileSync(path.join(logs, "axon-rfc.pid"), String(child.pid));
      const run = new Run(runDir, { pi, getCtx: () => latest, child, pid: child.pid });
      // Only this invocation's output; the logs of earlier attempts in a reused folder are history.
      run.files.out.offset = start;
      for (const k of ["progress", "activity"]) {
        try {
          run.files[k].offset = fs.statSync(path.join(logs, k === "progress" ? "progress.jsonl" : "activity.log")).size;
        } catch {}
      }
      watch(run);
      pi.sendMessage({
        customType: "rfc-agent",
        content: `rfc-agent started (typically 30–60 min): ${runDir}\nProgress shows above the editor; pauses open as questions here. ` +
          `It keeps running if you quit Axon — /rfc attach ${runDir} picks it up again.`,
        display: true,
      });
    },
  });
}
