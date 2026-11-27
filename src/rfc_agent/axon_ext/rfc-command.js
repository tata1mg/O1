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
 *   rating     `/rfc feedback [--run-dir R] [up|down]` (and one offer when a run ends) asks the questions
 *              commands/feedback.md asks, as Pi dialogs, and records them with `python -m rfc_agent.feedback`: this
 *              file only maps answers to that CLI's flags (feedbackArgs), it never scores anything.
 *
 * The child is detached and writes to files, so it outlives the Axon session; `/rfc attach <run dir>` picks a
 * running (or finished) run up again, including a pause that is still waiting.
 *
 * Asking the model about a run ("status?") should not need bash: Axon's terminal policy asks the user about
 * any command with `$`, redirects or globs. So this also registers `rfc_status` (a run's state, read-only) and
 * turns on Pi's read/grep/find/ls — what Read/Grep/Glob are in Claude Code. Bash approval stays Axon's.
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
  if (argValue(argv, "--engine") === "axon") return true;
  // every --stage-engine value, each a comma list like review=axon,draft=claude-code
  const values = argv.flatMap((a, i) => (a === "--stage-engine" ? [argv[i + 1] || ""]
    : a.startsWith("--stage-engine=") ? [a.slice("--stage-engine=".length)] : []));
  return values.some((v) => v.split(",").some((part) => part.split("=")[1]?.trim() === "axon"));
}

/** Whether Scope will fetch from Confluence/Jira (then the doctor also checks Axon's Atlassian sign-in). */
export function needsAtlassian(argv, cwd, exists = fs.existsSync) {
  if (argValue(argv, "--jira")) return true;
  const prd = argValue(argv, "--prd");
  return !!prd && !exists(path.resolve(cwd, prd));
}

const WAITING = /WAITING FOR (SELECTION|DESIGN|PAUSE (\d))[^:]*: proposal at (.+?) — answers expected at (.+?)\s*$/;

/** A `WAITING FOR …` line → {kind: "scope"|"design"|"investigate"|"triage", n, proposal, answers}, else null. */
export function parseWaiting(line) {
  const m = WAITING.exec(line);
  if (!m) return null;
  const n = m[2] ? Number(m[2]) : 0;
  const kind = m[1] === "DESIGN" ? "design" : !m[2] ? "scope" : n === 3 ? "triage" : "investigate";
  return { kind, n, proposal: m[3], answers: m[4] };
}

/** Whether `/rfc <text>` is a free-text prompt (not flags, not a subcommand): then rfc_agent.intake turns it into flags. */
export function isPrompt(text) {
  const first = (text || "").trim().split(/\s+/)[0] || "";
  return !!first && !first.startsWith("-") && !["doctor", "attach", "feedback", "help"].includes(first);
}

const FIGMA_LINK = /^https:\/\/(www\.)?figma\.com\/(design|file|proto)\/[A-Za-z0-9]{8,64}([/?#]|$)/;

const RELAY = [
  /\[rfc\] ── (.+?) ──/, // stage banner
  /\[rfc\] (done in .+)/,
  /\[rfc\] (STOPPED at .+)/,
  /\[\w+\] (STOP: .+)/, // a stage's own reason, printed just before the STOPPED line
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
  let fd;
  try {
    fd = fs.openSync(file, "r");
  } catch {
    return [];
  }
  try {
    const size = fs.fstatSync(fd).size; // of the file just opened, not a path that may have changed since
    if (size < state.offset) state.offset = 0; // truncated or replaced: start over
    if (size === state.offset) return [];
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
const REPOS_CONTINUE = "Continue with these repos";
const REPOS_ADD = "Add more repos / set a branch";

/** Toggle list on top of ui.select (Pi has no multi-select dialog). Escape keeps the current ticks. */
export async function checklist(ui, title, rows) {
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
  const extras = { add_repos: [], branches: {} };

  for (;;) {
    const repos = ofKind("repo");
    if (repos.length) {
      const proposed = repos.filter((r) => keep.has(r.n)).map((r) => r.label).join(", ") || "none";
      const on = await checklist(ui, `${header}\nWhich repos are in scope? Proposed: ${proposed}`,
        repos.map((r) => ({ label: r.label, note: r.note, on: keep.has(r.n) })));
      repos.forEach((r, i) => (on[i] ? keep.add(r.n) : keep.delete(r.n)));
    }
    const ticked = repos.filter((r) => keep.has(r.n)).map((r) => (r.branch ? `${r.label} (${r.branch})` : r.label)).join(", ") || "none";
    const choice = await ui.select(
      `Repos to trace (branch): ${ticked}\nDid the suggestions miss a repo, or do you need a different branch for one? (optional)`,
      [REPOS_CONTINUE, REPOS_ADD]);
    if (choice === REPOS_ADD) {
      const extra = await ui.input(
        "Comma-separated. To ADD a repo type its name: web_app_vite. To change the BRANCH of a repo, ticked or added, "
        + "type repo=branch: orders_service=release/2.1. Both together: web_app_vite, orders_service=release/2.1. "
        + "Added repos are traced like the ticked ones. Press Enter to skip.", "");
      const more = parseRepoExtras(extra);
      for (const r of more.add_repos) if (!extras.add_repos.includes(r)) extras.add_repos.push(r);
      Object.assign(extras.branches, more.branches);
    }
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
  const answers = { selected: [...keep].sort((a, b) => a - b), ...extras };
  if (prop.no_design) {
    const GO = "Proceed without design";
    const c = await ui.select(`No design is available (${prop.no_design}). Do you want to proceed as it is?`, [GO, "Stop"]);
    answers.proceed_without_design = c === GO;
  }
  return answers;
}

/** No Figma link came with the run: proceed without a design, stop, or give a link now. */
export async function askDesign(ui, prop) {
  const GO = "Proceed without design";
  const STOP = "Stop";
  const ADD = "Add your design (paste a Figma link)";
  const c = await ui.select(`No design is available (${prop.no_design}). What now?`, [GO, STOP, ADD]);
  if (c === GO) return { choice: "proceed" };
  if (c !== ADD) return { choice: "stop" };
  for (let tries = 0; tries < 3; tries++) {
    const url = ((await ui.input("Figma link (https://www.figma.com/design/<file key>/…)", "")) || "").trim();
    if (FIGMA_LINK.test(url)) return { choice: "add", figma_url: url };
    ui.notify("That is not a Figma design link.", "warning");
  }
  return { choice: "stop" };
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

// ---- rating: the questions of commands/feedback.md, answered as Pi dialogs --------------------------------

const NOTHING = "Nothing wrong";
const LAST = "picked last time";

/** The four areas asked by default: dimension, label, and the three gaps shown (feedback.DIMENSIONS values). */
export const AREAS = [
  { dim: "coverage", label: "Coverage", gaps: [
    ["missing_prd_flow", "Missing PRD flow"],
    ["missing_figma_flow_or_state", "Missing Figma flow or state"],
    ["marked_covered_but_not_addressed", "RFC says it's covered, but the software design in the RFC doesn't address it"]] },
  { dim: "buildability", label: "Buildability", gaps: [
    ["too_thin_to_build_from", "Too vague to start coding"],
    ["interface_or_contract_unclear", "Ambiguous interface or API contract"],
    ["build_order_or_dependency_missing", "Doesn't say what to build first or what depends on what"]] },
  { dim: "approach", label: "Approach", gaps: [
    ["poor_approach", "Poor approach for our systems, e.g. the wrong service or place to build it"],
    ["ignores_engineering_standards", "Ignores engineering standards"],
    ["tradeoff_not_justified", "Tradeoff not justified"]] },
  { dim: "context_layer", label: "Context layer use", gaps: [
    ["wrong_or_stale_code_fact", "Wrong or stale code fact"],
    ["missed_existing_code_or_service", "Missed existing code, service or API"],
    ["wrong_repos_or_pages", "Wrong repos or pages"]] },
];

export const STOP_REASONS = [
  ["too_slow", "It was taking too long"],
  ["too_costly", "Cost or tokens were too high"],
  ["wrong_repos_or_pages", "Wrong repos or pages were picked"],
  ["bad_input", "The PRD or Figma input was wrong"],
  ["changed_mind", "I changed my mind, or no longer need it"],
  ["other", "Other (type it)"],
];

export const FAILURE_REPORTS = [
  ["usage_or_login_limit", "A usage or login limit"],
  ["connector", "Atlassian, Figma or another connector"],
  ["connection", "Connection or network"],
  ["other", "Other (type it)"],
];

/** The checklist row for a gap that is not listed. It is Claude's built-in "Other" box: what is typed for it is a gap. */
export const OTHER_GAP = "Something else (type it)";

/** Ticks on an area's checklist (row 0 = Nothing wrong, then the listed gaps, then `Something else`) →
 *  {verdict, issues, other}. A gap beats `Nothing wrong`; no tick at all is an unanswered question: nothing is
 *  passed (and `--rerate` keeps the earlier answer). `other` is the unlisted gap: it has no issue name, because
 *  the CLI names it `other_see_comment` from the typed text. */
export function areaAnswer(area, on) {
  const issues = area.gaps.filter((_, i) => on[i + 1]).map(([v]) => v);
  const other = !!on[area.gaps.length + 1];
  if (issues.length || other) return { verdict: "usable_with_gaps", issues, other, contradicted: !!on[0] };
  if (on[0]) return { verdict: "usable", issues: [], other: false, contradicted: false };
  return { verdict: null, issues: [], other: false, contradicted: false };
}

/** A typed number of minutes → a whole number ≥ 0, else null (skip). */
export function parseMinutes(text) {
  const t = String(text ?? "").trim();
  return /^\d+$/.test(t) ? Number(t) : null;
}

/** The answers → argv for `python -m rfc_agent.feedback` (flags as commands/feedback.md §4 lists them). */
export function feedbackArgs(a) {
  const out = ["--run-dir", a.runDir];
  const add = (flag, v) => { if (v !== undefined && v !== null && v !== "") out.push(flag, String(v)); };
  add("--run-end", a.runEnd);
  add("--rating", a.rating);
  for (const [d, v] of Object.entries(a.verdicts || {})) out.push("--verdict", `${d}=${v}`);
  for (const [d, i] of a.issues || []) out.push("--issue", `${d}=${i}`);
  add("--stop-reason", a.stopReason);
  add("--failure-check", a.failureCheck);
  add("--failure-reported", a.failureReported);
  add("--rerun", a.rerun);
  add("--run-speed", a.runSpeed);
  add("--run-cost", a.runCost);
  add("--start-ready", a.startReady);
  add("--minutes-saved", a.minutesSaved);
  add("--minutes-spent", a.minutesSpent);
  if (a.minutesSkipped && a.minutesSaved == null && a.minutesSpent == null) out.push("--minutes-skipped");
  for (const [k, t] of Object.entries(a.notes || {})) if (String(t).trim()) out.push("--note", `${k}=${String(t).trim()}`);
  add("--comment", a.comment);
  add("--stage", a.stage);
  if (a.rerate) out.push("--rerate");
  if (a.preview) out.push("--preview");
  return out;
}

/** The newest run folder under `root` (a "~/" root is expanded), or null. */
export function newestRun(root, home = process.env.HOME || "") {
  const dir = root.startsWith("~/") ? path.join(home, root.slice(2)) : root;
  let best = null;
  try {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      if (!e.isDirectory()) continue;
      const p = path.join(dir, e.name);
      const t = fs.statSync(p).mtimeMs;
      if (!best || t > best.t) best = { p, t };
    }
  } catch {}
  return best?.p ?? null;
}

/** Every question of the rating form, in order. `uv` = (args) → {code,out,err} for `python -m rfc_agent.feedback`. */
export async function askFeedback(ui, ctxJson, { runDir, thumb, record, say }) {
  const prev = ctxJson.previous || null;
  const base = { runDir, rating: undefined, verdicts: {}, issues: [], notes: {} };
  let end = ctxJson.end;
  if (ctxJson.recorded) ui.notify(ctxJson.recorded, "info");
  const head = [ctxJson.recorded, ctxJson.line].filter(Boolean).join("\n");

  if (end === "unknown" || !["finished", "stopped", "failed"].includes(end)) {
    const c = await ui.select(`${head}\nHow did this run end?`, ["It finished", "I stopped it", "It failed", "Later"]);
    end = { "It finished": "finished", "I stopped it": "stopped", "It failed": "failed" }[c];
    if (!end) return say("No rating recorded. Run /rfc feedback any time.");
    base.runEnd = end;
  }

  const mark = (on) => (on ? ` (${LAST})` : "");
  const answers = { ...base };
  let changed = true;
  if (prev) {
    const bits = [prev.rating && (prev.rating === "up" ? "👍" : "👎"), prev.stop_reason, prev.failure_check && `cause ok: ${prev.failure_check}`,
      prev.rerun && `run again: ${prev.rerun}`, Object.keys(prev.verdicts || {}).length && `${Object.keys(prev.verdicts).length} areas`]
      .filter(Boolean).join(", ");
    const c = await ui.select(`${head}\nYou rated this run before: ${bits || "see the table"}. Keep or change?`, ["Keep it as it is", "Change some answers"]);
    if (c === undefined) return say("No rating recorded.");
    if (c === "Keep it as it is") changed = false;
    else ui.notify("A question you leave unanswered keeps your earlier answer. Pick another option to change it.", "info");
  }
  const wasLast = (v) => changed && prev && v;

  const ask = async () => {
    if (end === "finished") {
      if (!(await askFinished(ui, answers, prev, head, thumb, mark))) {
        say("No rating recorded. Run /rfc feedback any time after reading the RFC.");
        return false;
      }
    } else if (end === "stopped") {
      const labels = STOP_REASONS.map(([, l], i) => l + mark(wasLast(prev?.stop_reason === STOP_REASONS[i][0])));
      const c = await ui.select(`${head}\nWhat was the main reason you stopped it?`, labels);
      const hit = STOP_REASONS[labels.indexOf(c)];
      if (hit) {
        answers.stopReason = hit[0];
        if (hit[0] === "other") {
          const t = (await ui.input("Type the reason you stopped it", "")) || "";
          if (t.trim()) answers.notes.stop = t;
        }
      }
      await askRerun(ui, answers, prev, mark, wasLast);
    } else {
      const given = ctxJson.cause ? `${ctxJson.cause}${ctxJson.excerpt ? `\n${ctxJson.excerpt}` : ""}` : "";
      if (given) {
        const YES = "Yes, that is what happened";
        const NO = "No, something else happened";
        const c = await ui.select(`${head}\nIs that what happened?\n${given}`,
          [YES + mark(wasLast(prev?.failure_check === "yes")), NO + mark(wasLast(prev?.failure_check === "no"))]);
        if (c !== undefined) answers.failureCheck = c.startsWith(YES) ? "yes" : "no";
        if (answers.failureCheck === "no") {
          const t = (await ui.input("What happened instead? (optional)", "")) || "";
          if (t.trim()) answers.notes.failure = t;
        }
      } else {
        const labels = FAILURE_REPORTS.map(([, l], i) => l + mark(wasLast(prev?.failure_reported === FAILURE_REPORTS[i][0])));
        const c = await ui.select(`${head}\nWhat made it fail?`, labels);
        const hit = FAILURE_REPORTS[labels.indexOf(c)];
        if (hit) {
          answers.failureReported = hit[0];
          if (hit[0] === "other") {
            const t = (await ui.input("Type what made it fail", "")) || "";
            if (t.trim()) answers.notes.failure = t;
          }
        }
      }
      await askRerun(ui, answers, prev, mark, wasLast);
    }
    return true;
  };
  if (changed && !(await ask())) return;

  if (prev) {
    answers.rerate = true;
    for (;;) {
      const r = await record({ ...answers, preview: true });
      say(r);
      const c = await ui.select("Submit this?", ["Submit", "Change something"]);
      if (c === "Submit") break;
      if (c === undefined) return say("Nothing sent.");
      // Change something: ask again (the earlier picks stay as the answers of this round)
      if (!(await ask())) return;
    }
  }
  return say(await record(answers));
}

async function askRerun(ui, answers, prev, mark, wasLast) {
  const c = await ui.select("Would you run it again?", ["Yes" + mark(wasLast(prev?.rerun === "yes")), "No" + mark(wasLast(prev?.rerun === "no"))]);
  if (c !== undefined) answers.rerun = c.startsWith("Yes") ? "yes" : "no";
}

/** The finished form. Returns false when the person chose Later (or dismissed it). */
async function askFinished(ui, answers, prev, head, thumb, mark) {
  const prevThumb = prev?.rating === "up" ? (Object.keys(prev.verdicts || {}).length ? "notes" : "good") : prev?.rating === "down" ? "down" : null;
  {
    let t = thumb; // "up" | "down" from the command line
    if (!t) {
      const opts = [`👍 Good${mark(prevThumb === "good")}`, `👍 Good, with notes${mark(prevThumb === "notes")}`,
        `👎 Needs work${mark(prevThumb === "down")}`, "Later"];
      const c = await ui.select(`${head}\nHow was this RFC overall?`, opts);
      if (c === undefined || c === "Later") return false;
      t = c === opts[2] ? "down" : c === opts[0] ? "up" : "notes";
      answers.thumbKind = t;
    } else answers.thumbKind = t === "up" ? "up" : "down";
    answers.rating = t === "down" ? "down" : "up";
    if (answers.thumbKind === "up") return true; // a plain 👍 is one tap
  }

  for (const area of AREAS) {
    const was = prev?.verdicts?.[area.dim];
    const wasIssues = prev?.issues?.[area.dim] || [];
    const rows = [{ label: NOTHING, note: was === "usable" ? LAST : "", on: false },
      ...area.gaps.map(([v, l]) => ({ label: l, note: wasIssues.includes(v) ? LAST : "", on: false })),
      { label: OTHER_GAP, note: wasIssues.includes("other_see_comment") ? LAST : "", on: false }];
    const on = await checklist(ui, `${area.label}: anything wrong? (pick any)`, rows);
    const r = areaAnswer(area, on);
    if (r.contradicted) ui.notify(`${area.label} had both Nothing wrong and a gap ticked; recorded the gap`, "info");
    if (r.verdict) {
      answers.verdicts[area.dim] = r.verdict;
      answers.issues = answers.issues.filter(([d]) => d !== area.dim);
      r.issues.forEach((i) => answers.issues.push([area.dim, i]));
    }
    // A note on an area is a gap to the scorer, so ask for one only where a gap was ticked: for the unlisted gap
    // (what it is), or as an optional addition to the listed ones. Never after `Nothing wrong`.
    let note = "";
    if (r.other) note = ((await ui.input(`${area.label}: describe the gap`, "")) || "").trim();
    else if (r.issues.length) note = ((await ui.input(`${area.label}: anything to add about it? (optional)`, "")) || "").trim();
    if (note) answers.notes[area.dim] = note;
  }
  if (answers.verdicts.buildability === "usable") answers.startReady = "yes";

  const mins = async (q, was) => {
    const hint = changedHint(was);
    return parseMinutes(await ui.input(`${q}${hint}`, ""));
  };
  const saved = await mins("How many minutes did this RFC save you versus writing it yourself? (a number, or leave empty to skip)", prev?.minutes_saved);
  const spent = await mins("After the RFC was generated, how many minutes did you spend reviewing and correcting it yourself? " +
    "(a number, or leave empty to skip; leave out the run itself and any pauses)", prev?.minutes_spent);
  if (saved !== null) answers.minutesSaved = saved;
  if (spent !== null) answers.minutesSpent = spent;
  if (saved === null && spent === null && !prev) answers.minutesSkipped = true;
  return true;
}

const changedHint = (was) => (was != null ? `\nLast time: ${was} minutes. A number to change it, or leave it` : "");

// ---- rfc_status: a run's state as a read-only tool -------------------------------------------------------

export function tail(file, n) {
  try {
    return fs.readFileSync(file, "utf8").split("\n").filter((l) => l.trim()).slice(-n);
  } catch {
    return null;
  }
}

export function pidAlive(pid) {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** What `rfc_status` reports for one run folder: process, pending pause, status.json, log tails, outputs. */
export function runSnapshot(dir, { outLines = 30, activityLines = 15 } = {}) {
  const logs = path.join(dir, "logs");
  if (!fs.existsSync(logs)) throw new Error(`${dir} is not an rfc-agent run folder (no logs/)`);
  const parts = [`run: ${dir}`];
  let pid = 0;
  try {
    pid = Number(fs.readFileSync(path.join(logs, "axon-rfc.pid"), "utf8"));
  } catch {}
  const alive = pidAlive(pid);
  parts.push(`process: ${pid ? (alive ? `running (pid ${pid})` : `not running (pid ${pid} ended)`) : "no pid file"}`);
  let out = [];
  try {
    out = fs.readFileSync(path.join(logs, "run.out"), "utf8").split("\n");
  } catch {}
  const waiting = out.map(parseWaiting).filter((w) => w && !fs.existsSync(w.answers));
  if (waiting.length && alive) {
    const w = waiting[waiting.length - 1];
    parts.push(`waiting: ${w.kind === "scope" ? "Scope selection" : w.kind === "design" ? "the design question" : `pause ${w.n}`} — the /rfc dialog asks it; proposal at ${w.proposal}`);
  }
  try {
    parts.push(`status.json:\n${fs.readFileSync(path.join(logs, "status.json"), "utf8").trim()}`);
  } catch {}
  const runOut = out.filter((l) => l.trim()).slice(-outLines);
  if (runOut.length) parts.push(`run.out (last ${runOut.length} lines):\n${runOut.join("\n")}`);
  const act = tail(path.join(logs, "activity.log"), activityLines);
  if (act?.length) parts.push(`activity.log (last ${act.length} lines):\n${act.join("\n")}`);
  const outputs = ["out/rfc.readable.md", "out/rfc.md", "review/review.md"].filter((f) => fs.existsSync(path.join(dir, f)));
  if (outputs.length) parts.push(`outputs: ${outputs.map((f) => path.join(dir, f)).join(", ")}`);
  return parts.join("\n\n");
}

/** Pi's built-in tools that only read. Not bash, so Axon's terminal policy never asks about them — the same
 *  footing Read/Grep/Glob have in Claude Code. */
export const READ_ONLY_TOOLS = ["read", "grep", "find", "ls"];

export function withReadOnlyTools(active, registered) {
  const add = READ_ONLY_TOOLS.filter((t) => registered.includes(t) && !active.includes(t));
  return add.length ? [...active, ...add] : null;
}

// ---- one watched run ----------------------------------------------------------------------------------

class Run {
  constructor(runDir, { pi, getCtx, child, pid, offerRating }) {
    this.offerRating = offerRating;
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
      if (/^(done in|STOPPED at|STOP:|read:|full:)/.test(t)) this.closing.push(t);
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
      const label = w.kind === "scope" ? "Scope selection" : w.kind === "design" ? "the design question" : `pause ${w.n}`;
      ui.notify(`rfc ${this.name}: ${label} is waiting for you`, "info");
      const data = w.kind === "scope" ? await askScope(ui, prop)
        : w.kind === "design" ? await askDesign(ui, prop)
        : w.kind === "triage" ? await askTriage(ui, prop)
        : await askInvestigate(ui, prop, w.n);
      if (!fs.existsSync(w.answers)) writeAnswers(w.answers, data);
      this.pending.delete(w.answers);
      this.push(`answered ${label}`);
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
      : `Continue it with /rfc --run-dir ${this.dir} — finished stages are kept. (/rfc --prd again starts a new run.)`;
    this.pi.sendMessage({
      customType: "rfc-agent",
      content: `rfc-agent run ${this.dir}\n${body}\n${tail}`,
      display: true,
    });
    // Once, like the Claude Code command: a run that ended either way can be rated (stopped and failed have their own questions).
    if (ui && this.offerRating) this.offerRating(this.dir).catch((e) => ui.notify(`rfc: rating failed (${e.message})`, "error"));
  }
}

// ---- the extension ------------------------------------------------------------------------------------

export function runCapture(cmd, args, opts) {
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
export function childEnv(home = process.env.HOME || "", exists = fs.existsSync) {
  const env = { ...process.env };
  for (const k of Object.keys(env)) if (k.startsWith("PI_")) delete env[k];
  // The pipeline's agents start `axon pi` themselves; Axon launched from a shell that predates its installer's
  // PATH line would otherwise make every stage fail with "`axon` not found".
  const bin = path.join(home, ".axon", "bin");
  const dirs = (env.PATH || "").split(path.delimiter);
  if (home && !dirs.includes(bin) && exists(path.join(bin, "axon"))) env.PATH = [bin, ...dirs.filter(Boolean)].join(path.delimiter);
  return env;
}

const HELP = [
  "/rfc --prd <confluence url | page id | file.md> [--figma <link>] [--instructions <text | file>] [flags]   run the pipeline (agents on Axon by default)",
  "/rfc Build an rfc from <PRD link> and <anything the PRD should not say>   the same, in plain words (a PRD link is required)",
  "/rfc --run-dir <run> [--from <stage>]                      continue a stopped run (or redo a stage onward)",
  "/rfc attach <run dir>                                     follow a run started elsewhere (or answer its pause)",
  "/rfc feedback [--run-dir <run>] [up|down]                 rate a run (finished, stopped or failed); saved on its Langfuse trace",
  "/rfc doctor                                               check Axon, models and fleetlens",
  "Flags are the rfc CLI's: --jira, --repos, --no-figma, --preset, --ask, --ask-review, --yes, --langfuse-verbose, --engine, --stage-engine.",
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
    lastDir = run.dir;
    run.render();
    if (!timer) timer = setInterval(tick, POLL_MS);
  };
  const uvRun = (args, cwd) => runCapture(uv, ["run", "--project", project, ...args], { cwd, env: childEnv() });

  // ---- rating ----

  const feedbackCli = (args, cwd) => uvRun(["python", "-m", "rfc_agent.feedback", ...args], cwd);

  /** The questions, then the record: `rfc_agent.feedback` does all the scoring. */
  async function rate(runDir, { thumb } = {}) {
    const ui = latest?.ui;
    if (!ui) return;
    const cwd = latest.cwd || process.cwd();
    const c = await feedbackCli(["--run-dir", runDir, "--context"], cwd);
    let ctxJson;
    try {
      ctxJson = JSON.parse(c.out.trim().split("\n").pop());
    } catch {
      ui.notify(`rfc: could not read ${runDir} for rating: ${(c.err || c.out).trim().split("\n").pop()}`, "error");
      return;
    }
    const say = (text) => text && pi.sendMessage({ customType: "rfc-agent", content: text, display: true });
    ui.notify(`rfc: rating ${path.basename(runDir)}`, "info");
    await askFeedback(ui, ctxJson, {
      runDir, thumb, say,
      record: async (answers) => {
        const r = await feedbackCli(feedbackArgs(answers), cwd);
        const text = (r.out + r.err).trim();
        return r.code === 0 ? text : `rfc: the rating was not recorded (exit ${r.code})\n${text}`;
      },
    });
  }

  /** The one offer at the end of a run. */
  async function offerRating(runDir) {
    const ui = latest?.ui;
    if (!ui) return;
    const c = await ui.select(`Rate this run? (${path.basename(runDir)}) It lands on the run's Langfuse trace.`, ["Rate it now", "Later"]);
    if (c === "Rate it now") await rate(runDir);
    else ui.notify(`rfc: rate it any time with /rfc feedback --run-dir ${runDir}`, "info");
  }

  let lastDir = null; // the run rfc_status reports when it is given none
  const watchedDir = () => [...runs.keys()].pop() || lastDir;

  pi.on("session_start", (_e, ctx) => {
    latest = ctx;
    if (typeof pi.getActiveTools !== "function" || typeof pi.setActiveTools !== "function") return;
    const registered = (pi.getAllTools?.() || []).map((t) => t.name);
    const next = withReadOnlyTools(pi.getActiveTools(), registered);
    if (next) pi.setActiveTools(next);
  });

  pi.registerTool({
    name: "rfc_status",
    label: "rfc status",
    description:
      "State of an rfc-agent run: whether its process is alive, a pause waiting for the user, logs/status.json, " +
      "the tail of logs/run.out and logs/activity.log, and which outputs exist. Read-only.",
    promptGuidelines: [
      "For questions about an rfc-agent run (status, progress, what it is doing, cost so far), call rfc_status " +
        "instead of running shell commands; use read, grep, find and ls for anything else in the run folder.",
      "Never write a pause's answers file yourself: the /rfc dialog asks the user.",
    ],
    parameters: {
      type: "object",
      properties: {
        run_dir: { type: "string", description: "The run folder. Omit for the run /rfc is following in this session." },
      },
    },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
    async execute(_id, params, _signal, _onUpdate, ctx) {
      const given = params?.run_dir;
      const dir = given ? path.resolve(ctx?.cwd || process.cwd(), given) : watchedDir();
      if (!dir) throw new Error("no rfc run is being followed in this session; pass run_dir");
      return { content: [{ type: "text", text: runSnapshot(dir) }], details: undefined };
    },
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
      ["--prd ", "attach ", "doctor", "feedback ", "--run-dir ", "--from ", "--engine ", "--stage-engine ", "--ask", "--ask-review", "--yes"]
        .filter((c) => c.startsWith(prefix))
        .map((c) => ({ value: c, label: c.trim() })),
    handler: async (args, ctx) => {
      latest = ctx;
      const ui = ctx.ui;
      let argv = splitArgs(args);
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
        const r = await uvRun(["python", "-m", "rfc_agent.axon_agent", "doctor", "--table"], ctx.cwd);
        ui.setWorkingMessage?.();
        pi.sendMessage({ customType: "rfc-agent", content: (r.out + r.err).trim(), display: true });
        return;
      }

      if (argv[0] === "feedback") {
        const rest = argv.slice(1);
        let dir = argValue(rest, "--run-dir");
        if (dir) dir = path.resolve(ctx.cwd, dir);
        else dir = watchedDir();
        if (!dir) {
          const r = await uvRun(["python", "-c", "import rfc_agent.config as c; print(c.get_config().get('RUNS_ROOT') or '~/rfc-runs')"], ctx.cwd);
          dir = newestRun(r.out.trim().split("\n").pop() || "~/rfc-runs");
        }
        if (!dir || !fs.existsSync(path.join(dir, "logs"))) {
          ui.notify("rfc: /rfc feedback --run-dir <run folder> — no run folder found to rate", "error");
          return;
        }
        ui.notify(`rfc: rating ${dir}`, "info");
        await rate(dir, { thumb: rest.find((a) => a === "up" || a === "down") });
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
        watch(new Run(dir, { pi, getCtx: () => latest, pid, offerRating }));
        ui.notify(`rfc: following ${dir}`, "info");
        return;
      }

      if (isPrompt(args)) { // free text ("Build an rfc from <PRD link> and …") → flags, by rfc_agent.intake
        ui.setWorkingMessage?.("rfc: reading your request…");
        const r = await uvRun(["python", "-m", "rfc_agent.intake", "--json", "--", args], ctx.cwd);
        ui.setWorkingMessage?.();
        let parsed = null;
        try { parsed = JSON.parse(r.out.trim().split("\n").pop()).argv; } catch { /* reported below */ }
        if (r.code !== 0 || !Array.isArray(parsed)) {
          pi.sendMessage({ customType: "rfc-agent", content: (r.err || r.out || "rfc: could not read that request").trim(), display: true });
          return;
        }
        argv = parsed;
        const figmaLinks = argv.flatMap((a, i) => (a === "--figma" ? [argv[i + 1]] : []));
        pi.sendMessage({ customType: "rfc-agent", display: true, content:
          `rfc: reading this as — PRD: ${argValue(argv, "--prd")}` + (figmaLinks.length ? ` · Figma: ${figmaLinks.join(", ")}` : "")
          + (argValue(argv, "--instructions") ? " · with your instructions" : "") });
      }

      const full = withEngine(argv);
      if (usesAxon(full)) {
        ui.setWorkingMessage?.("rfc: checking Axon…");
        const doc = ["python", "-m", "rfc_agent.axon_agent", "doctor"];
        if (!needsAtlassian(full, ctx.cwd)) doc.push("--no-atlassian");
        const d = await uvRun(doc, ctx.cwd);
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
      const fd = fs.openSync(outFile, "a");
      const start = fs.fstatSync(fd).size;
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
      const run = new Run(runDir, { pi, getCtx: () => latest, child, pid: child.pid, offerRating });
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
