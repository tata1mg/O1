// Drives rfc-command.js (the /rfc command for Axon) under plain node, with a fake Pi, a fake UI and a fake uv
// (fake_uv.py) standing in for the pipeline. Usage:
//   node rfc_command_cases.mjs <rfc-command.js> <fake_uv.py> <project dir> <run dir> <cwd>
// Prints one JSON line {ok, failures:[...]} — the python test asserts on it.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [, , cmdJs, fakeUv, project, runDir, cwd] = process.argv;
const m = await import(pathToFileURL(cmdJs).href);
const failures = [];
const check = (name, cond, got) => { if (!cond) failures.push({ name, got }); };

// ---- pure helpers
check("splitArgs quotes", JSON.stringify(m.splitArgs(`--prd "a b.md" --jira 'X-1' --yes`)) ===
  JSON.stringify(["--prd", "a b.md", "--jira", "X-1", "--yes"]), m.splitArgs(`--prd "a b.md" --jira 'X-1' --yes`));
check("withEngine adds axon", m.withEngine(["--prd", "x"]).join(" ") === "--prd x --engine axon");
check("withEngine keeps a chosen engine", m.withEngine(["--engine", "claude-code"]).join(" ") === "--engine claude-code");
check("withEngine keeps a stage map", m.withEngine(["--stage-engine", "review=axon"]).length === 2);
check("usesAxon stage map", m.usesAxon(["--stage-engine", "review=axon"]) && !m.usesAxon(["--engine", "claude-code"]));
check("needsAtlassian: confluence url, jira, local file, rerun",
  m.needsAtlassian(["--prd", "https://x.atlassian.net/wiki/pages/1"], "/") && m.needsAtlassian(["--jira", "AB-1"], "/") &&
  !m.needsAtlassian(["--prd", "prd.md"], "/w", () => true) && !m.needsAtlassian(["--run-dir", "/r", "--from", "review"], "/"));
check("argValue = form", m.argValue(["--run-dir=/r"], "--run-dir") === "/r");
const w = m.parseWaiting("  [investigate] WAITING FOR PAUSE 3: proposal at /r/confirm/p3.proposal.json — answers expected at /r/confirm/p3.answers.json");
check("parseWaiting triage", w && w.kind === "triage" && w.n === 3 && w.answers === "/r/confirm/p3.answers.json", w);
const s = m.parseWaiting("  [scope] WAITING FOR SELECTION: proposal at /r/confirm/proposal.json — answers expected at /r/confirm/answers.json");
check("parseWaiting scope", s && s.kind === "scope" && s.proposal === "/r/confirm/proposal.json", s);
check("relayLine banner", m.relayLine("  [rfc] ── Review ──") === "Review");
check("relayLine keeps a stage's STOP reason",
  m.relayLine("  [scope] STOP: no Atlassian MCP server is connected") === "STOP: no Atlassian MCP server is connected");
check("relayLine ignores noise", m.relayLine("  [investigate] tracer T1 call 4") === null);
check("progressText", m.progressText('{"text": "Plan: 3 flows"}') === "Plan: 3 flows" && m.progressText("nope") === null);
check("parseRepoExtras", JSON.stringify(m.parseRepoExtras("a, b=feature/x ,c")) ===
  JSON.stringify({ add_repos: ["a", "c"], branches: { b: "feature/x" } }));
check("parseNotes", JSON.stringify(m.parseNotes("R-F1.2: disagree; bad; R-X.1: ok")) ===
  JSON.stringify({ "R-F1.2": "disagree", "R-X.1": "ok" }));

// ---- the whole command against the fake pipeline
const handlers = {};
const commands = {};
const messages = [];
const notes = [];
const asked = [];
let widget = null;
const ui = {
  async select(title, options) {
    asked.push({ title: title.split("\n").pop(), options });
    if (/Which repos/.test(title) && options.some((o) => o.startsWith("[x] svc_b"))) {
      return options.find((o) => o.startsWith("[x] svc_b")); // untick svc_b, then Done on the next round
    }
    if (/Retry these repo snapshots/.test(title) && options.some((o) => o.startsWith("[ ] svc_c"))) {
      return options.find((o) => o.startsWith("[ ] svc_c"));
    }
    if (/Who owns cart/.test(title)) return "cart team";
    return options[0]; // Done / Plan looks right
  },
  async input(title) {
    if (/Add repos/.test(title)) return "svc_new, svc_a=feature/x";
    if (/Notes on findings/.test(title)) return "R-F1.2: disagree, svc_a never edits";
    return "";
  },
  notify: (msg, type) => notes.push({ msg, type }),
  setWidget: (_k, lines) => { widget = lines; },
  setStatus: () => {},
  setWorkingMessage: () => {},
};
const ctx = { cwd, hasUI: true, ui };
const pi = {
  on: (name, fn) => { handlers[name] = fn; },
  registerCommand: (name, opts) => { commands[name] = opts; },
  sendMessage: (msg) => messages.push(msg),
};
m.default(pi, { project, uv: fakeUv });
check("registers /rfc", !!commands.rfc);
handlers.session_start({}, ctx);

// preflight failure: nothing starts
process.env.FAKE_UV_DOCTOR_FAIL = "1";
await commands.rfc.handler("--prd x.md", ctx);
delete process.env.FAKE_UV_DOCTOR_FAIL;
check("doctor failure stops the run", messages.length === 1 && /preflight failed/.test(messages[0].content) &&
  !fs.existsSync(path.join(runDir, "argv.json")), messages);

process.env.FAKE_UV_RUN_DIR = runDir;
process.env.PI_SECRET_RUNTIME = "x";
await commands.rfc.handler(`--prd x.md`, ctx);
const done = await new Promise((resolve) => {
  const t0 = Date.now();
  const iv = setInterval(() => {
    const fin = messages.find((x) => /done in/.test(x.content));
    if (fin || Date.now() - t0 > 40000) { clearInterval(iv); resolve(fin); }
  }, 200);
});
handlers.session_shutdown?.({}, ctx);

const read = (f) => { try { return JSON.parse(fs.readFileSync(path.join(runDir, f), "utf8")); } catch { return null; } };
const argv = read("argv.json");
check("CLI got --engine axon, --run-dir and --confirm-file",
  argv && argv.argv.join(" ") === `--prd x.md --engine axon --run-dir ${runDir} --confirm-file`, argv);
check("no PI_* runtime vars reach the pipeline", argv && argv.pi_env.length === 0, argv);
check("scope answers", JSON.stringify(read("got-scope.json")) ===
  JSON.stringify({ selected: [1], add_repos: ["svc_new"], branches: { svc_a: "feature/x" } }), read("got-scope.json"));
check("pause 1 answers", JSON.stringify(read("got-investigate-pause1.json")) ===
  JSON.stringify({ selected: [1, 2], feedback: "", answers: { Q1: "cart team" } }), read("got-investigate-pause1.json"));
check("triage answers", JSON.stringify(read("got-investigate-pause3.json")) ===
  JSON.stringify({ selected: [1], notes: { "R-F1.2": "disagree, svc_a never edits" }, feedback: "" }),
  read("got-investigate-pause3.json"));
check("finish posts the closing lines", done && /read: .*rfc\.readable\.md/.test(done.content), messages.map((x) => x.content));
check("widget cleared at the end", widget === undefined, widget);
check("pid file written", fs.existsSync(path.join(runDir, "logs", "axon-rfc.pid")));
check("milestones reached the widget or log", fs.readFileSync(path.join(runDir, "logs", "run.out"), "utf8").includes("── Review"));

console.log(JSON.stringify({ ok: failures.length === 0, failures }));
process.exit(0);
