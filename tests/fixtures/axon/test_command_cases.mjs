// Drives test-command.js (the /test command for Axon) under plain node, with a fake Pi, a fake UI and a fake uv
// (fake_uv_testing.py) standing in for the helpers and the harness. Usage:
//   node test_command_cases.mjs <test-command.js> <fake_uv_testing.py> <project dir> <run dir> <cwd> <uv log>
// Prints one JSON line {ok, failures:[...]} — the python test asserts on it.
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const [, , cmdJs, fakeUv, project, runDir, cwd, uvLog] = process.argv;
process.env.FAKE_RUN_DIR = runDir;
process.env.FAKE_UV_LOG = uvLog;
process.env.PI_PARENT_ONLY = "1"; // must not leak into the children
const m = await import(pathToFileURL(cmdJs).href);
const failures = [];
const check = (name, cond, got) => { if (!cond) failures.push({ name, got }); };

// ---- pure helpers
check("parseTestArgs sub", JSON.stringify(m.parseTestArgs(["execute", "web"])) === JSON.stringify({ sub: "execute", argv: ["web"] }));
check("parseTestArgs run", m.parseTestArgs(["--feature", "X"]).sub === "run");
check("engineArgs default axon", m.engineArgs(["--feature", "X"]).join(" ") === "--engine axon");
check("engineArgs keeps the user's", m.engineArgs(["--stage-engine", "derive_ui=axon"]).join(" ") === "--stage-engine derive_ui=axon");
check("withoutEngine", m.withoutEngine(["a", "--engine", "x", "--stage-engine=y=z", "b"]).join(" ") === "a b");
check("engineFor", m.engineFor([]) === "axon" && m.engineFor(["--engine", "claude-code"]) === "claude-code");
check("parseEvent started", m.parseEvent("[10:00:01] [generate_cases] STAGE started").text.startsWith("▶ generate_cases — writing the suite"));
check("parseEvent revising", m.parseEvent("[10:00:01] [generate_cases] gate FAILED (3 failures) — automatic revision next").type === "revising");
check("parseEvent progress", m.parseEvent("PROGRESS generate_cases · 14m of usually 19–22m · writing cases: 61").text ===
  "⏳ generate_cases · 14m of usually 19–22m · writing cases: 61");
check("parseEvent rearm", m.parseEvent("WATCH rearm --from 1234").offset === 1234);
const tri = m.triageChecks([{ id: "kiwi", level: "fail", msg: "unreachable" }, { id: "rfc", level: "fail", msg: "no rfc" }, { id: "x", level: "warn", msg: "w" }]);
check("triageChecks", tri.kiwiDown && tri.stop.length === 1 && tri.warnings.length === 1 && !tri.kiwiSetup, tri);
check("triageChecks json storage", m.triageChecks([{ id: "kiwi", level: "fail", msg: "testing.storage is 'json'" }]).kiwiSetup);
const web = m.harnessCmd("web", { feature: "F", runDir: "/r", rfcRunDir: "/d", env: "e", build: "b", baseUrl: "https://u", uiLogin: "/l", headed: true, engine: ["--engine", "axon"] });
check("harnessCmd web", web.join(" ") === "harness run testing --feature F --run-dir /r --rfc-run-dir /d --env e --build b --no-interactive " +
  "--stages derive_ui,execute_web --enable testing.execute_web --base-url https://u --ui-login /l --enable testing.ui_headed --engine axon", web.join(" "));
const gen = m.harnessCmd("generate", { feature: "F", runDir: "/r", rfcRunDir: "/d", overrides: ["--test-data", "/t"], engine: ["--engine", "axon"], noStore: true });
check("harnessCmd generate", gen.join(" ") === "harness run testing --feature F --run-dir /r --rfc-run-dir /d --no-interactive --test-data /t --engine axon --disable testing.store", gen.join(" "));
check("parsePrint missing", JSON.stringify(m.parsePrint("execute web: missing --env, --build").missing) === JSON.stringify(["--env", "--build"]));
check("parsePrint run dir", m.parsePrint("uv run --project P harness run testing --feature F --run-dir /x/y --env e").runDir === "/x/y");
const rc = await import(pathToFileURL(cmdJs.replace("test-command.js", "rfc-command.js")).href);
const saved = process.env.PATH;
process.env.PATH = "/usr/bin";
check("childEnv adds ~/.axon/bin when axon is there", rc.childEnv("/h", (p) => p === "/h/.axon/bin/axon").PATH === "/h/.axon/bin:/usr/bin");
check("childEnv leaves PATH alone otherwise", rc.childEnv("/h", () => false).PATH === "/usr/bin");
process.env.PATH = saved;
check("failureHints net", m.failureHints({ results_web: { not_passing: [{ status: "BLOCKED", detail: "net::ERR_NAME_NOT_RESOLVED" }] } }).length === 1);

// ---- fake Pi
const handlers = {};
const commands = {};
const tools = {};
const messages = [];
const asked = [];
const notes = [];
const ui = {
  async select(title, options) {
    asked.push({ title: title.split("\n")[0], options });
    if (/Which kinds should run/.test(title)) {
      const be = options.find((o) => o.startsWith("[x] Backend"));
      return be || options[0]; // untick Backend, then Done
    }
    if (/Watch it run/.test(title)) return "Run in the background";
    if (/newly confirmed web case/.test(title)) return options[0]; // run the existing now
    return options[0];
  },
  async input(title, placeholder) {
    asked.push({ title, input: true });
    if (/^Build/.test(title)) return "b1";
    return placeholder || "";
  },
  notify: (t, l) => notes.push({ t, l }),
  setWidget() {},
  setStatus() {},
  setWorkingMessage() {},
};
const ctx = { cwd, hasUI: true, ui };
const pi = {
  on: (e, f) => (handlers[e] = f),
  registerCommand: (n, spec) => (commands[n] = spec),
  registerTool: (spec) => (tools[spec.name] = spec),
  sendMessage: (msg) => messages.push(msg.content),
};
m.default(pi, { project, uv: fakeUv });
check("registers /test and test_status", !!commands.test && !!tools.test_status);
handlers.session_start?.({}, ctx);

const waitFor = async (re, ms = 30000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    const hit = messages.find((x) => re.test(x));
    if (hit) return hit;
    await new Promise((r) => setTimeout(r, 100));
  }
  return null;
};
const calls = () => fs.readFileSync(uvLog, "utf8").trim().split("\n").map((l) => JSON.parse(l));

// ---- /test help
await commands.test.handler("help", ctx);
check("help", messages.some((x) => x.includes("/test execute")));

// ---- /test: phase A, the handoff, phase B (web)
await commands.test.handler("", ctx);
const handoff = await waitFor(/🔔 Cases are ready for review/);
check("handoff posted", handoff && handoff.includes("12 cases in 3 modules") && handoff.includes("https://kiwi/plan/9/") &&
  handoff.includes("not scored yet") && handoff.includes("test_data.template.yaml"), handoff);
check("milestones posted", messages.some((x) => x.startsWith("✓ generate_cases: fine")), messages);
const results = await waitFor(/^Results —/);
check("phase B report", results && results.includes("web UI: 2 PASS · 1 FAIL — https://kiwi/runs/3/") &&
  results.includes("TC-3 FAIL: Error: locator not found") && results.includes("show-report"), results);
const harness = calls().filter((c) => c.argv[0] === "harness");
const g = harness[0]?.argv.join(" ") || "";
check("generate launched with axon", /--feature HDPO .*--rfc-run-dir \/rfc\/prd-1 --no-interactive --engine axon$/.test(g), g);
const w = harness[1]?.argv.join(" ") || "";
check("web launched with env, build, base URL, login, axon",
  w.includes("--env jupiter --build b1") && w.includes("--stages derive_ui,execute_web") &&
  w.includes("--base-url https://jupiter.example.com --ui-login /secret/login.json --engine axon") && !w.includes("ui_headed"), w);
check("backend was unticked", !harness.some((c) => c.argv.includes("execute_be")), harness.map((c) => c.argv.join(" ")));
check("check ran with --engine axon", calls().some((c) => c.argv.includes("check") && c.argv.join(" ").includes("--engine axon")));
check("no PI_ vars reach the children", calls().every((c) => c.pi_env.length === 0), calls().map((c) => c.pi_env));
check("derive_ui's skipped cases posted", messages.some((x) => x.includes("derive_ui left 1 case(s)") && x.includes("TC-9")),
  messages.filter((x) => x.includes("derive_ui")));

// ---- /test execute: pending → run the existing now
messages.length = 0;
await commands.test.handler("execute web", ctx);
const exReport = await waitFor(/^Results —/);
check("execute reported", !!exReport, messages);
const ex = calls().filter((c) => c.argv[2] === "workflows.testing.execute" && !c.argv.includes("--print") && !c.argv.includes("--pending")).pop();
check("execute ran --only-existing on axon", ex && ex.argv.includes("--only-existing") && ex.argv.join(" ").includes("--engine axon"), ex);
check("execute asked pending first", asked.some((a) => /newly confirmed web case/.test(a.title)));

// ---- /test evaluate
messages.length = 0;
await commands.test.handler("evaluate", ctx);
const ev = await waitFor(/^Score 3.8\/5/);
check("evaluate report", ev && ev.includes("boundary coverage — 2 violation(s)") && ev.includes("Missing scenarios found: 7"), ev);
const evc = calls().filter((c) => c.argv[2] === "workflows.testing.evaluation" && !c.argv.includes("--print")).pop();
check("evaluate on axon", evc && evc.argv.join(" ").includes("--engine axon"), evc);

// ---- test_status
const snap = await tools.test_status.execute("1", { run_dir: runDir }, null, null, ctx);
check("test_status", snap.content[0].text.includes("status --json:") && snap.content[0].text.includes("activity.log"), snap.content[0].text.slice(0, 300));

handlers.session_shutdown?.();
console.log(JSON.stringify({ ok: failures.length === 0, failures }));
process.exit(0);
