// Drives rfc-tools.js (and the guard's isGranted) under plain node. Usage:
//   node rfc_tools_cases.mjs <rfc-tools.js> <harness-guard.js> <python> <hook.py> <run_dir>
// Prints one JSON line {ok, failures:[...]} — the python test asserts on it.
import { mkdirSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [, , toolsJs, guardJs, python, hookPy, runDir] = process.argv;
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

// the real hook.py, end to end: the same allow-list as on Claude
mkdirSync(path.join(runDir, "out"), { recursive: true });
process.env.INVESTIGATE_RUN_DIR = runDir;
process.env.INVESTIGATE_WRITE_ALLOW = "out/*";
const real = { cmd: [python, hookPy] };
const inside = t.preDecision(real, "write", { path: "out/ok.md" }, runDir);
check("real hook allows a write inside the allow-list", !inside.block, inside);
const outside = t.preDecision(real, "write", { path: "secret.md" }, runDir);
check("real hook denies a write outside it", outside.block && /DENIED: you may only write out\/\*/.test(outside.reason), outside);

console.log(JSON.stringify({ ok: failures.length === 0, failures }));
