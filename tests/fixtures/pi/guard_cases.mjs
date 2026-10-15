/**
 * Node test driver for pi_ext/harness-guard.js.
 *
 * `decide()` is a pure function of (tool, input, policy) plus an injected policy caller, precisely so the
 * decision table can be tested here without pi and without Python — and the last block then runs the REAL
 * `python -m harness.policy check` to prove the two sides agree on the JSON contract.
 *
 * argv: node guard_cases.mjs <abs path to harness-guard.js> <policy cmd as JSON array>
 * Exits 0 with "ok <n> cases" on success, 1 with the failures on stderr.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const [guardPath, policyCmdJson] = process.argv.slice(2);
const { decide, recordDenial, callPolicy, CANONICAL_TOOL } = await import(guardPath);

const RUN = mkdtempSync(path.join(tmpdir(), "pi-guard-"));
const basePolicy = {
  cwd: RUN,
  writable_roots: [RUN],
  readable_roots: ["/repos"],
  readonly_shell: true,
  disabled_tools: [],
  denials_path: "",
  denials_echo: false,
  cmd: JSON.parse(policyCmdJson || "[]"),
};

const failures = [];
let ran = 0;
function testCase(name, fn) {
  ran += 1;
  try {
    fn();
  } catch (err) {
    failures.push(`${name}: ${err && err.message ? err.message : err}`);
  }
}

/** A stub policy caller that records what it was asked and answers with a fixed verdict. */
function stub(verdict, seen = {}) {
  return (request) => {
    Object.assign(seen, request);
    return verdict;
  };
}

// ---- the verdict mapping ---------------------------------------------------------------------------

testCase("allow passes through", () => {
  const d = decide({ tool: "read", input: { path: "raw/prd.md" }, policy: basePolicy,
                     call: stub({ decision: "allow", reason: "inside" }) });
  assert.deepEqual(d, { block: false });
});

testCase("deny blocks and relays the policy's reason verbatim", () => {
  const d = decide({ tool: "write", input: { path: "/etc/hosts" }, policy: basePolicy,
                     call: stub({ decision: "deny", reason: "Write targets /etc/hosts, outside the run dir." }) });
  assert.equal(d.block, true);
  assert.equal(d.reason, "Write targets /etc/hosts, outside the run dir.");
});

testCase("ask blocks headless, and says why", () => {
  const d = decide({ tool: "bash", input: { command: "make build" }, policy: basePolicy,
                     call: stub({ decision: "ask", reason: "not provably read-only" }) });
  assert.equal(d.block, true);
  assert.match(d.reason, /nobody to ask/);
});

testCase("ask on a shell call is allowed when the harness turned classification off", () => {
  const policy = { ...basePolicy, readonly_shell: false };
  const d = decide({ tool: "bash", input: { command: "make build" }, policy,
                     call: stub({ decision: "ask", reason: "read-only shell classification disabled" }) });
  assert.deepEqual(d, { block: false });
});

testCase("ask on a WRITE still blocks even with classification off", () => {
  const policy = { ...basePolicy, readonly_shell: false };
  const d = decide({ tool: "write", input: { path: "x" }, policy,
                     call: stub({ decision: "ask", reason: "no opinion" }) });
  assert.equal(d.block, true);
});

testCase("a policy call that throws BLOCKS (fail safe)", () => {
  const d = decide({ tool: "bash", input: { command: "ls" }, policy: basePolicy,
                     call: () => { throw new Error("python is gone"); } });
  assert.equal(d.block, true);
  assert.match(d.reason, /policy check failed \(blocking\)/);
  assert.match(d.reason, /python is gone/);
});

testCase("a disabled tool is blocked without consulting the policy at all", () => {
  const policy = { ...basePolicy, disabled_tools: ["bash"] };
  const d = decide({ tool: "bash", input: { command: "ls" }, policy,
                     call: () => { throw new Error("must not be called"); } });
  assert.equal(d.block, true);
  assert.match(d.reason, /is disabled for this stage/);
});

testCase("an unrecognised tool defaults to DENY via the policy's 'no rule' ask", () => {
  const seen = {};
  const d = decide({ tool: "deploy", input: { target: "prod" }, policy: basePolicy,
                     call: stub({ decision: "ask", reason: "no policy rule for tool 'deploy'" }, seen) });
  assert.equal(seen.tool, "deploy", "an unmapped tool keeps its native name");
  assert.equal(d.block, true);
});

// ---- the request this guard builds ------------------------------------------------------------------

testCase("pi tool names are mapped to the names the policy routes on", () => {
  assert.equal(CANONICAL_TOOL.write, "Write");
  assert.equal(CANONICAL_TOOL.edit, "Edit");
  assert.equal(CANONICAL_TOOL.read, "Read");
  assert.equal(CANONICAL_TOOL.bash, "Bash");
  assert.equal(CANONICAL_TOOL.grep, "Read", "a search is judged by READ confinement");
  assert.equal(CANONICAL_TOOL.ls, "Read");
});

testCase("paths are RESOLVED, never substring-matched", () => {
  const seen = {};
  decide({ tool: "write", input: { path: "../../etc/hosts" }, policy: basePolicy,
           call: stub({ decision: "allow" }, seen) });
  assert.equal(seen.input.path, path.resolve(RUN, "../../etc/hosts"));
  assert.ok(path.isAbsolute(seen.input.path));
  assert.ok(!seen.input.path.includes(".."), "a traversal must be collapsed before the policy sees it");
});

testCase("the full policy envelope is forwarded", () => {
  const seen = {};
  decide({ tool: "bash", input: { command: "ls -la" }, policy: basePolicy, call: stub({ decision: "allow" }, seen) });
  assert.equal(seen.tool, "Bash");
  assert.equal(seen.native_tool, "bash");
  assert.deepEqual(seen.input, { command: "ls -la" });
  assert.equal(seen.cwd, RUN);
  assert.deepEqual(seen.writable_roots, [RUN]);
  assert.deepEqual(seen.readable_roots, ["/repos"]);
  assert.equal(seen.readonly_shell, true);
});

testCase("a search with no root is judged against the cwd it would walk", () => {
  const seen = {};
  decide({ tool: "grep", input: { pattern: "REQ-\\d+" }, policy: basePolicy, call: stub({ decision: "allow" }, seen) });
  assert.equal(seen.input.path, RUN);
  assert.equal(seen.input.pattern, "REQ-\\d+");
});

testCase("recordDenial appends one JSON line and never throws on a bad path", () => {
  const denials = path.join(RUN, "denials.jsonl");
  recordDenial({ tool: "write", toolCallId: "c1", reason: "nope" }, { ...basePolicy, denials_path: denials });
  const rec = JSON.parse(readFileSync(denials, "utf8").trim());
  assert.equal(rec.type, "harness_denial");
  assert.equal(rec.tool, "write");
  assert.equal(rec.toolCallId, "c1");
  recordDenial({ tool: "x" }, { ...basePolicy, denials_path: "/nonexistent-dir/denials.jsonl" });
});

testCase("an unmapped tool forwards only whitelisted keys, so a stray `command` cannot look like a shell", () => {
  const seen = {};
  decide({ tool: "deploy", input: { command: "rm -rf /", target: "prod", path: "x" }, policy: basePolicy,
           call: stub({ decision: "ask", reason: "no policy rule" }, seen) });
  assert.equal(seen.input.command, undefined, "a `command` key must not smuggle an unknown tool into the shell rule");
  assert.equal(seen.input.target, undefined);
  assert.ok(seen.input.path.endsWith("x"));
});

testCase("read confinement the harness never declared is not invented", () => {
  // no readable_roots -> the policy answers "not enforced" as an ask, and Claude's transport defers too.
  const policy = { ...basePolicy, readable_roots: [] };
  for (const tool of ["read", "grep", "find", "ls"]) {
    const d = decide({ tool, input: { path: "/somewhere/else/README.md" }, policy,
                       call: stub({ decision: "ask", reason: "no readable roots declared" }) });
    assert.deepEqual(d, { block: false }, tool);
  }
});

testCase("declared readable roots DO make an ask a denial again", () => {
  const d = decide({ tool: "read", input: { path: "/somewhere/else" }, policy: basePolicy,
                     call: stub({ decision: "ask", reason: "no opinion" }) });
  assert.equal(d.block, true);
});

testCase("an explicit enforced:false verdict is an allowance", () => {
  const d = decide({ tool: "write", input: { path: "x" }, policy: basePolicy,
                     call: stub({ decision: "ask", enforced: false, reason: "not enforced" }) });
  assert.deepEqual(d, { block: false });
});

// ---- against the REAL python policy ----------------------------------------------------------------

if (basePolicy.cmd.length) {
  testCase("REAL policy: a write outside the run dir is denied", () => {
    const d = decide({ tool: "write", input: { path: "/etc/hosts" }, policy: basePolicy, call: callPolicy });
    assert.equal(d.block, true);
    assert.match(d.reason, /outside the run dir/);
  });

  testCase("REAL policy: a write inside the run dir is allowed", () => {
    const d = decide({ tool: "write", input: { path: "out/rfc.md" }, policy: basePolicy, call: callPolicy });
    assert.deepEqual(d, { block: false });
  });

  testCase("REAL policy: a mutating shell command is denied", () => {
    const d = decide({ tool: "bash", input: { command: "curl https://example.com -o x" }, policy: basePolicy,
                       call: callPolicy });
    assert.equal(d.block, true);
  });

  testCase("REAL policy: a read outside every declared root is denied", () => {
    const d = decide({ tool: "read", input: { path: "/Users/someone/.ssh/id_rsa" }, policy: basePolicy,
                       call: callPolicy });
    assert.equal(d.block, true);
  });

  testCase("REAL policy: the pi scratchpad is NOT a writable exemption", () => {
    // /tmp/pi-* was briefly exempt for every engine; nothing observed pi creating such a dir.
    const d = decide({ tool: "write", input: { path: "/tmp/pi-anything/y" }, policy: basePolicy,
                       call: callPolicy });
    assert.equal(d.block, true, "an injected instruction could otherwise name this tree");
  });
}

if (failures.length) {
  console.error(`FAILED ${failures.length}/${ran}:\n  ` + failures.join("\n  "));
  process.exit(1);
}
console.log(`ok ${ran} cases`);
