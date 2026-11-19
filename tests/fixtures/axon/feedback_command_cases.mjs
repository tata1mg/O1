// Drives `/rfc feedback` (rfc-command.js) under plain node: scripted Pi dialogs, fake uv (fake_uv.py) standing in for
// `python -m rfc_agent.feedback`. Usage:
//   node feedback_command_cases.mjs <rfc-command.js> <fake_uv.py> <project dir> <work dir>
// Prints one JSON line {ok, failures:[...]} — the python test asserts on it.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [, , cmdJs, fakeUv, project, work] = process.argv;
const m = await import(pathToFileURL(cmdJs).href);
const failures = [];
const check = (name, cond, got) => { if (!cond) failures.push({ name, got }); };

// ---- pure helpers
const area = m.AREAS.find((a) => a.dim === "coverage");
check("areaAnswer: Nothing wrong", JSON.stringify(m.areaAnswer(area, [true, false, false, false])) ===
  JSON.stringify({ verdict: "usable", issues: [], contradicted: false }));
check("areaAnswer: a gap beats Nothing wrong", JSON.stringify(m.areaAnswer(area, [true, false, true, false])) ===
  JSON.stringify({ verdict: "usable_with_gaps", issues: ["missing_figma_flow_or_state"], contradicted: true }));
check("areaAnswer: nothing ticked is unanswered", m.areaAnswer(area, [false, false, false, false]).verdict === null);
check("parseMinutes", m.parseMinutes("30") === 30 && m.parseMinutes(" 0 ") === 0 && m.parseMinutes("skip") === null &&
  m.parseMinutes("-3") === null && m.parseMinutes("") === null && m.parseMinutes(undefined) === null);
check("feedbackArgs finished", m.feedbackArgs({ runDir: "/r", rating: "down", verdicts: { coverage: "usable_with_gaps" },
  issues: [["coverage", "missing_prd_flow"]], notes: { coverage: "see flow 3", run: "  " }, minutesSkipped: true, rerate: true, preview: true })
  .join("|") === "--run-dir|/r|--rating|down|--verdict|coverage=usable_with_gaps|--issue|coverage=missing_prd_flow|--minutes-skipped|--note|coverage=see flow 3|--rerate|--preview");
check("feedbackArgs never mixes skipped with minutes", !m.feedbackArgs({ runDir: "/r", minutesSaved: 5, minutesSkipped: true }).includes("--minutes-skipped"));
check("feedbackArgs stopped", m.feedbackArgs({ runDir: "/r", runEnd: "stopped", stopReason: "other", rerun: "no", notes: { stop: "wrong env" } })
  .join("|") === "--run-dir|/r|--run-end|stopped|--stop-reason|other|--rerun|no|--note|stop=wrong env");
const runs = path.join(work, "runs");
for (const [n, t] of [["old", 1000], ["new", 2000]]) {
  fs.mkdirSync(path.join(runs, n), { recursive: true });
  fs.utimesSync(path.join(runs, n), t, t);
}
check("newestRun", m.newestRun(runs) === path.join(runs, "new") && m.newestRun(path.join(work, "none")) === null);

// ---- the command, one scripted session per case
const logFile = path.join(work, "feedback.log");
process.env.FAKE_UV_FEEDBACK_LOG = logFile;
process.env.FAKE_UV_RUNS_ROOT = runs;
const handlers = {};
const commands = {};
const messages = [];
const asked = [];
let script = [];
const ui = {
  async select(title, options) {
    asked.push({ title, options });
    const step = script.shift();
    if (!step) { failures.push({ name: "unscripted select", got: title }); return undefined; }
    const [re, pick] = step;
    if (!re.test(title)) failures.push({ name: "script order", got: { title, expected: String(re) } });
    if (typeof pick === "function") return pick(options);
    const hit = options.find((o) => o === pick || o.startsWith(pick));
    if (hit === undefined && pick !== undefined) failures.push({ name: "no such option", got: { pick, options } });
    return hit;
  },
  async input(title) {
    asked.push({ title, options: null });
    const step = script.shift();
    if (!step) { failures.push({ name: "unscripted input", got: title }); return ""; }
    if (!step[0].test(title)) failures.push({ name: "script order", got: { title, expected: String(step[0]) } });
    return step[1];
  },
  notify: () => {},
  setWidget: () => {}, setStatus: () => {}, setWorkingMessage: () => {},
};
const ctx = { cwd: work, hasUI: true, ui };
const pi = {
  on: (n, fn) => { handlers[n] = fn; },
  registerCommand: (n, o) => { commands[n] = o; },
  sendMessage: (msg) => messages.push(msg),
  registerTool: () => {},
};
m.default(pi, { project, uv: fakeUv });

async function session(name, context, steps, args = "") {
  const run = path.join(work, name);
  fs.mkdirSync(path.join(run, "logs"), { recursive: true });
  fs.writeFileSync(path.join(work, "ctx.json"), JSON.stringify({ line: "$1.00, 40 min", recorded: `Recorded: ${name}`, earlier: [], previous: null, ...context }));
  process.env.FAKE_UV_CONTEXT = path.join(work, "ctx.json");
  fs.rmSync(logFile, { force: true });
  messages.length = 0;
  asked.length = 0;
  script = steps;
  await commands.rfc.handler(`feedback --run-dir ${run} ${args}`, ctx);
  check(`${name}: every scripted answer was asked`, script.length === 0, script.map((s) => String(s[0])));
  const calls = fs.existsSync(logFile) ? fs.readFileSync(logFile, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
  return { calls, run };
}
const tick = (...labels) => (opts) => opts.find((o) => labels.some((l) => o.includes(l)));
const runDirArgs = (run) => ["--run-dir", run];

// finished, 👎 with notes, a gap, a typed note, both time questions
{
  const r = await session("finished", { end: "finished" }, [
    [/How was this RFC overall/, "👎 Needs work"],
    [/Coverage: anything wrong/, "[ ] Missing PRD flow"], [/Coverage: anything wrong/, "✔ Done"], [/Coverage: anything else/, "flow 3 is missing too"],
    [/Buildability: anything wrong/, "[ ] Nothing wrong"], [/Buildability: anything wrong/, "✔ Done"], [/Buildability: anything else/, ""],
    [/Approach: anything wrong/, "✔ Done"], [/Approach: anything else/, ""],
    [/Context layer use: anything wrong/, "✔ Done"], [/Context layer use: anything else/, ""],
    [/How many minutes did this RFC save/, "45"], [/how many minutes did you spend reviewing/, "skip"],
  ]);
  check("finished: one record, the CLI's flags", r.calls.length === 1 && JSON.stringify(r.calls[0]) === JSON.stringify([
    ...runDirArgs(r.run), "--rating", "down", "--verdict", "coverage=usable_with_gaps", "--verdict", "buildability=usable",
    "--issue", "coverage=missing_prd_flow", "--start-ready", "yes", "--minutes-saved", "45", "--note", "coverage=flow 3 is missing too"]), r.calls);
  check("finished: recorded line shown first", asked[0].title.includes("Recorded: finished") && asked[0].title.includes("$1.00"), asked[0]);
  check("finished: the CLI's output is shown as printed", messages.some((x) => x.content.startsWith("feedback recorded: saved locally")), messages);
}
// finished, a plain 👍 is one tap
{
  const r = await session("finished-up", { end: "finished" }, [[/How was this RFC overall/, "👍 Good"]]);
  check("👍 Good: only the thumb", r.calls.length === 1 && JSON.stringify(r.calls[0]) === JSON.stringify([...runDirArgs(r.run), "--rating", "up"]), r.calls);
}
// the `up` argument skips the question; `Later` records nothing
{
  const r = await session("finished-arg", { end: "finished" }, [], "up");
  check("up argument", r.calls.length === 1 && r.calls[0].includes("up") && !asked.length, r.calls);
  const l = await session("finished-later", { end: "finished" }, [[/How was this RFC overall/, "Later"]]);
  check("Later records nothing", l.calls.length === 0 && messages.some((x) => /No rating recorded/.test(x.content)), messages);
}
// stopped: reason + rerun, no thumb
{
  const r = await session("stopped", { end: "stopped" }, [
    [/What was the main reason you stopped it/, "Other (type it)"], [/Type the reason/, "VPN dropped"], [/Would you run it again/, "Yes"]]);
  check("stopped", r.calls.length === 1 && JSON.stringify(r.calls[0]) === JSON.stringify([
    ...runDirArgs(r.run), "--stop-reason", "other", "--rerun", "yes", "--note", "stop=VPN dropped"]), r.calls);
  check("stopped: no thumb or areas asked", !asked.some((a) => /overall|anything wrong/.test(a.title)));
}
// failed with a recorded cause
{
  const r = await session("failed", { end: "failed", cause: "connection_error", excerpt: "timeout talking to Confluence" }, [
    [/Is that what happened\?\nconnection_error\ntimeout talking to Confluence/, "No, something else"], [/What happened instead/, "disk full"],
    [/Would you run it again/, "No"]]);
  check("failed", r.calls.length === 1 && JSON.stringify(r.calls[0]) === JSON.stringify([
    ...runDirArgs(r.run), "--failure-check", "no", "--rerun", "no", "--note", "failure=disk full"]), r.calls);
}
// failed, killed (no cause recorded)
{
  const r = await session("failed-nocause", { end: "failed", cause: "" }, [
    [/What made it fail/, "Connection or network"], [/Would you run it again/, "Yes"]]);
  check("failed without a cause", JSON.stringify(r.calls[0]) === JSON.stringify([...runDirArgs(r.run), "--failure-reported", "connection", "--rerun", "yes"]), r.calls);
}
// unknown end: asked once, passed as --run-end
{
  const r = await session("unknown", { end: "unknown", recorded: "" }, [
    [/How did this run end/, "I stopped it"], [/main reason/, "It was taking too long"], [/run it again/, "No"]]);
  check("unknown end", JSON.stringify(r.calls[0]) === JSON.stringify([...runDirArgs(r.run), "--run-end", "stopped", "--stop-reason", "too_slow", "--rerun", "no"]), r.calls);
}
// re-rating: keep → preview → submit; change → marks, preview, change something, submit
{
  const prevStop = { stop_reason: "too_slow", rerun: "no", source: "run folder" };
  const k = await session("rerate-keep", { end: "stopped", previous: prevStop }, [
    [/You rated this run before: too_slow, run again: no/, "Keep it as it is"], [/Submit this\?/, "Submit"]]);
  check("keep: --rerate only, preview first", k.calls.length === 2 && JSON.stringify(k.calls[0]) === JSON.stringify([...runDirArgs(k.run), "--rerate", "--preview"]) &&
    JSON.stringify(k.calls[1]) === JSON.stringify([...runDirArgs(k.run), "--rerate"]), k.calls);
  check("keep: the preview table is shown", messages.some((x) => x.content.includes("preview, nothing sent")), messages);

  const c = await session("rerate-change", { end: "stopped", previous: prevStop }, [
    [/rated this run before/, "Change some answers"],
    [/main reason/, (o) => { check("earlier reason marked", o.find((x) => x.startsWith("It was taking too long"))?.endsWith("(picked last time)"), o); return o.find((x) => x.startsWith("Cost or tokens")); }],
    [/run it again/, (o) => { check("earlier rerun marked", o.find((x) => x.startsWith("No"))?.endsWith("(picked last time)"), o); return undefined; }],
    [/Submit this\?/, "Change something"],
    [/main reason/, "Cost or tokens"], [/run it again/, "Yes"], [/Submit this\?/, "Submit"]]);
  check("change: preview, preview, final", c.calls.length === 3 && c.calls[0].includes("--preview") && c.calls[1].includes("--preview") && !c.calls[2].includes("--preview") &&
    c.calls[2].includes("--rerate") && c.calls[2].includes("too_costly") && c.calls[2].includes("yes"), c.calls);
  check("change: a question left unanswered passes nothing", !c.calls[0].includes("--rerun"), c.calls[0]);

  const f = await session("rerate-finished", { end: "finished", previous: { rating: "up", verdicts: { coverage: "usable_with_gaps" }, issues: { coverage: ["missing_prd_flow"] }, minutes_saved: 30 } }, [
    [/rated this run before/, "Change some answers"],
    [/How was this RFC overall/, (o) => { check("thumb marked", o.find((x) => x.startsWith("👍 Good, with notes"))?.endsWith("(picked last time)"), o); return o.find((x) => x.startsWith("👍 Good, with notes")); }],
    [/Coverage: anything wrong/, (o) => { check("gap marked", o.find((x) => x.includes("Missing PRD flow"))?.includes("picked last time"), o); return "✔ Done"; }],
    [/Coverage: anything else/, ""],
    [/Buildability: anything wrong/, "✔ Done"], [/Buildability: anything else/, ""], [/Approach: anything wrong/, "✔ Done"], [/Approach: anything else/, ""],
    [/Context layer use: anything wrong/, "✔ Done"], [/Context layer use: anything else/, ""],
    [/save you/, (o) => undefined], [/spend reviewing/, ""], [/Submit this\?/, "Submit"]]);
  check("re-rating a finished run keeps unanswered answers (nothing passed for them)", f.calls.length === 2 &&
    JSON.stringify(f.calls[1]) === JSON.stringify([...runDirArgs(f.run), "--rating", "up", "--rerate"]), f.calls);
  check("earlier minutes are shown", asked.some((a) => /Last time: 30 minutes/.test(a.title)), asked.map((a) => a.title));
}
// no --run-dir: the newest run folder under RUNS_ROOT
{
  fs.mkdirSync(path.join(runs, "new", "logs"), { recursive: true });
  fs.writeFileSync(path.join(work, "ctx.json"), JSON.stringify({ end: "stopped", line: "", recorded: "", previous: null }));
  fs.rmSync(logFile, { force: true });
  script = [[/main reason/, "I changed my mind"], [/run it again/, "No"]];
  await commands.rfc.handler("feedback", ctx);
  const call = JSON.parse(fs.readFileSync(logFile, "utf8").trim());
  check("newest run under RUNS_ROOT", call[1] === path.join(runs, "new"), call);
}
check("registered completion offers feedback", commands.rfc.getArgumentCompletions("fe").some((c) => c.value === "feedback "));

console.log(JSON.stringify({ ok: failures.length === 0, failures }));
process.exit(0);
