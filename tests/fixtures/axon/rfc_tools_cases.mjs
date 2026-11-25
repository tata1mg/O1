// Drives rfc-tools.js (and the guard's isGranted) under plain node. Usage:
//   node rfc_tools_cases.mjs <rfc-tools.js> <harness-guard.js> <python> <hook.py> <run_dir>
// Prints one JSON line {ok, failures:[...]} — the python test asserts on it.
import { mkdirSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [, , toolsJs, guardJs, python, hookPy, runDir, browserGuardPy] = process.argv;
const t = await import(pathToFileURL(toolsJs).href);
const g = await import(pathToFileURL(guardJs).href);
const failures = [];
const check = (name, cond, got) => { if (!cond) failures.push({ name, got }); };

// input mapping
const ci = t.claudeInput("write", { path: "out/a.md", content: "x" }, "/run");
check("write path -> absolute file_path", ci.file_path === "/run/out/a.md" && ci.content === "x", ci);
const cg = t.claudeInput("grep", { pattern: "foo", path: "_snap" }, "/run");
check("grep keeps pattern and an absolute path", cg.pattern === "foo" && cg.path === "/run/_snap", cg);

// pre: exit codes
const hook = { cmd: ["py", "hook.py"] };
const deny = t.preDecision(hook, "write", { path: "x" }, "/run", () => ({ status: 2, stderr: "DENIED: nope\n" }));
check("exit 2 blocks with the hook's message", deny.block && deny.reason === "DENIED: nope", deny);
const allow = t.preDecision(hook, "edit", { path: "x" }, "/run", () => ({ status: 0 }));
check("exit 0 allows", !allow.block, allow);
const broken = t.preDecision(hook, "write", { path: "x" }, "/run", () => ({ error: new Error("ENOENT") }));
check("a hook that cannot run blocks the write", broken.block && /blocking/.test(broken.reason), broken);
let called = false;
const read = t.preDecision(hook, "read", { path: "x" }, "/run", () => { called = true; return { status: 2 }; });
check("reads never go through the write check", !read.block && !called, read);
check("no hook configured = no write check", !t.preDecision(null, "write", { path: "x" }, "/run").block, null);

// post: additionalContext
const ctx = JSON.stringify({ hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext: "wrap up now" } });
check("post returns additionalContext",
  t.postFeedback(hook, "read", {}, false, "/run", () => ({ status: 0, stdout: ctx + "\n" })) === "wrap up now", null);
check("post ignores garbage", t.postFeedback(hook, "read", {}, false, "/run", () => ({ status: 0, stdout: "{" })) === "", null);
check("post ignores a failing hook", t.postFeedback(hook, "read", {}, false, "/run", () => ({ status: 1, stdout: ctx })) === "", null);

// guard grants
check("granted exact", g.isGranted("mcp__fleetlens__find_symbol", ["mcp__fleetlens__find_symbol"]), null);
check("granted prefix", g.isGranted("mcp__fleetlens__x", ["mcp__fleetlens__*"]), null);
check("not granted", !g.isGranted("mcp__other__x", ["mcp__fleetlens__*"]), null);
check("only mcp tools can be granted", !g.isGranted("bash", ["bash"]), null);
const policy = { cwd: "/run", disabled_tools: [], granted_tools: ["mcp__fleetlens__find_symbol"], readable_roots: [] };
const v = g.decide({ tool: "mcp__fleetlens__find_symbol", input: {}, policy, call: () => { throw new Error("no"); } });
check("a granted MCP tool passes the guard without a policy call", !v.block, v);
const v2 = g.decide({ tool: "mcp__fleetlens__get_symbol", input: {}, policy, call: () => ({ decision: "ask" }) });
check("an ungranted MCP tool is still default-denied", v2.block, v2);

// codemode (issue #16): grantable by exact name only, and only when the stage granted it
check("codemode granted by name", g.isGranted("codemode", ["mcp__fleetlens__*", "codemode"]), null);
check("codemode not granted unless listed", !g.isGranted("codemode", ["mcp__fleetlens__*"]), null);
check("no wildcard reaches codemode", !g.isGranted("codemode", ["*", "code*"]), null);
check("granting codemode does not grant bash", !g.isGranted("bash", ["codemode", "bash"]), null);
const cm = g.decide({ tool: "codemode", input: { code: "x" }, policy: { ...policy, granted_tools: ["codemode"] },
                      call: () => { throw new Error("no"); } });
check("a granted codemode passes the guard", !cm.block, cm);
const cm2 = g.decide({ tool: "codemode", input: {}, policy, call: () => ({ decision: "ask" }) });
check("an ungranted codemode is default-denied", cm2.block, cm2);

// hook notes of calls a codemode script made go to the script's own result, once each
const pend = new Map();
const inner = t.liftNotes(pend, { toolCallId: "p/1", parentToolCallId: "p", content: [{ type: "text", text: "{}" }] }, "12 calls used");
check("an inner result is left untouched", inner === undefined, inner);
t.liftNotes(pend, { toolCallId: "p/2", parentToolCallId: "p" }, "12 calls used");
t.liftNotes(pend, { toolCallId: "p/3", parentToolCallId: "p" }, "");
t.liftNotes(pend, { toolCallId: "q/1", parentToolCallId: "q" }, "other script");
const outer = t.liftNotes(pend, { toolCallId: "p", content: [{ type: "text", text: "Script completed" }] }, "");
const text = outer && outer.content.map((b) => b.text).join("");
check("the script's result carries its inner notes, each once",
  outer && outer.content.length === 2 && text.split("12 calls used").length === 2 && !/other script/.test(text), outer);
check("a delivered script's notes are cleared", !pend.has("p") && pend.has("q"), [...pend.keys()]);
const plain = t.liftNotes(pend, { toolCallId: "r", content: [] }, "own note");
check("a direct call still gets its own note", plain && /own note/.test(plain.content[0].text), plain);
check("no note = unchanged", t.liftNotes(pend, { toolCallId: "s", content: [] }, "") === undefined, null);

// the real hook.py, end to end: the same allow-list as on Claude
mkdirSync(path.join(runDir, "out"), { recursive: true });
process.env.INVESTIGATE_RUN_DIR = runDir;
process.env.INVESTIGATE_WRITE_ALLOW = "out/*";
const real = { cmd: [python, hookPy] };
const inside = t.preDecision(real, "write", { path: "out/ok.md" }, runDir);
check("real hook allows a write inside the allow-list", !inside.block, inside);
const outside = t.preDecision(real, "write", { path: "secret.md" }, runDir);
check("real hook denies a write outside it", outside.block && /DENIED: you may only write out\/\*/.test(outside.reason), outside);

// tool guards: a PreToolUse script per tool-name prefix, failing closed
const guards = [{ prefix: "mcp__playwright__", cmd: ["py", "guard.py"] }];
let seen = null;
const gAllow = t.guardDecision(guards, "mcp__playwright__browser_click", { element: "Add" }, "/run",
  (cmd, payload) => { seen = { cmd, payload }; return { status: 0 }; });
check("guard exit 0 allows", !gAllow.block, gAllow);
check("the guard gets the Claude PreToolUse payload", seen && seen.cmd[1] === "guard.py" &&
  seen.payload.hook_event_name === "PreToolUse" && seen.payload.tool_name === "mcp__playwright__browser_click" &&
  seen.payload.tool_input.element === "Add", seen);
const gDeny = t.guardDecision(guards, "mcp__playwright__browser_navigate", { url: "x" }, "/run",
  () => ({ status: 2, stderr: "DENIED by rfc_agent browser-guard: off origin\n" }));
check("guard exit 2 blocks with its message", gDeny.block && gDeny.reason === "DENIED by rfc_agent browser-guard: off origin", gDeny);
const gCrash = t.guardDecision(guards, "mcp__playwright__browser_click", {}, "/run", () => ({ status: 1, stderr: "Traceback" }));
check("a guard that crashes blocks", gCrash.block && /blocking/.test(gCrash.reason), gCrash);
const gGone = t.guardDecision(guards, "mcp__playwright__browser_click", {}, "/run", () => ({ error: new Error("ENOENT") }));
check("a guard that cannot run blocks", gGone.block && /blocking/.test(gGone.reason), gGone);
let asked = false;
const gOther = t.guardDecision(guards, "mcp__fleetlens__find_symbol", {}, "/run", () => { asked = true; return { status: 2 }; });
check("a tool outside the prefix is not the guard's", !gOther.block && !asked, gOther);

// write checks: the check's output rides on the write's result
const checks = [{ path: "/run/qa/agent/TC-1/verdict.json", cmd: ["py", "check.py", "qa/agent/TC-1"] }];
const said = (out) => () => ({ status: 1, stdout: out, stderr: "" });
check("a write to the checked file returns what the check printed",
  t.writeCheck(checks, "write", { path: "qa/agent/TC-1/verdict.json" }, false, "/run", said("REJECTED — 1 problem\n")) === "REJECTED — 1 problem", null);
check("an edit of it is checked too",
  t.writeCheck(checks, "edit", { path: "/run/qa/agent/TC-1/verdict.json" }, false, "/run", said("OK")) === "OK", null);
check("another file is not checked", t.writeCheck(checks, "write", { path: "qa/agent/TC-1/notes.md" }, false, "/run", said("OK")) === "", null);
check("a failed write is not checked", t.writeCheck(checks, "write", { path: "qa/agent/TC-1/verdict.json" }, true, "/run", said("OK")) === "", null);
check("a read is not checked", t.writeCheck(checks, "read", { path: "qa/agent/TC-1/verdict.json" }, false, "/run", said("OK")) === "", null);

// the real guard_browser.py, end to end: the same origin lock as on Claude
if (browserGuardPy) {
  process.env.RFC_AGENT_BROWSER_ORIGIN = "https://web.test";
  const realGuards = [{ prefix: "mcp__playwright__", cmd: [python, browserGuardPy] }];
  const on = t.guardDecision(realGuards, "mcp__playwright__browser_snapshot", {}, runDir);
  check("real browser guard allows a look at the page", !on.block, on);
  const off = t.guardDecision(realGuards, "mcp__playwright__browser_navigate", { url: "https://evil.example/" }, runDir);
  check("real browser guard denies a navigation off the run's origin", off.block && /outside this run's test environment/.test(off.reason), off);
  const evalJs = t.guardDecision(realGuards, "mcp__playwright__browser_evaluate", { function: "() => 1" }, runDir);
  check("real browser guard denies a tool the stage does not grant", evalJs.block && /not one of the browser tools/.test(evalJs.reason), evalJs);
}

console.log(JSON.stringify({ ok: failures.length === 0, failures }));
