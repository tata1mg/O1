/**
 * harness-guard — Pi's containment transport (D6).
 *
 * Pi ships NO permission system: "it runs with the permissions of the user and process that launched it"
 * (pi 0.85.1 README). The one interception point is `pi.on("tool_call")`, which fires after
 * tool_execution_start and before the tool runs, can block, can mutate the input, and — the property this
 * whole design leans on — FAILS SAFE: a handler that throws blocks the call.
 *
 * The POLICY does not live here. It lives once, in Python (`harness.policy`), and the Claude adapter
 * transports the same policy through PreToolUse hooks. This file is a translator: native Pi tool name +
 * input -> the policy CLI's JSON contract -> a Pi block verdict. Two copies of the path/shell rules would
 * drift, and a ~50 ms subprocess per tool call is cheaper than that drift (D6).
 *
 * Environment contract, all set by src/harness/engines/pi.py:
 *   HARNESS_POLICY_CMD      JSON array argv, e.g. ["/abs/python","-m","harness.policy","check"]
 *   HARNESS_POLICY_JSON     {writable_roots, readable_roots, readonly_shell, disabled_tools, cwd}
 *   HARNESS_DENIALS_PATH    JSONL side channel this file appends one record per block to
 *   HARNESS_DENIALS_ECHO    "1" -> ALSO console.log the record (see the note below)
 *
 * Why the denials side file and NOT the event stream: measured against pi 0.85.1 on 2026-09-08, an
 * extension's `console.log` in `--mode json` is routed to **stderr**, not into the JSONL on stdout. So an
 * extension cannot put a record into the stream at all; a file we own is the only channel, and it has the
 * further virtue of being deterministic and unable to corrupt the JSONL a downstream `jq` consumer reads.
 * The echo stays behind a flag for a human tailing the stderr side file while a stage runs.
 */

import { spawnSync } from "node:child_process";
import { appendFileSync } from "node:fs";
import path from "node:path";

/**
 * Native Pi tool name -> the engine-neutral (Claude-spelled) name `harness.policy` routes on.
 * Pi's built-ins at 0.85.1 are read, bash, powershell, edit, write, grep, find, ls.
 * grep/find/ls map onto Read because what the policy has to decide about them is read confinement:
 * "does the root this search walks sit inside a declared readable root". Anything NOT in this table is
 * passed through under its native name, where the policy answers "no policy rule for tool X" (ask) and
 * the mapping below turns that into a block — default-deny for unrecognised tools, on purpose.
 */
export const CANONICAL_TOOL = Object.freeze({
  write: "Write",
  edit: "Edit",
  multiedit: "MultiEdit",
  read: "Read",
  grep: "Read",
  find: "Read",
  ls: "Read",
  bash: "Bash",
  powershell: "Bash",
});

/** Native tool -> the key in `input` holding the path it acts on. Absent = no path (shell). */
const PATH_KEY = Object.freeze({
  write: "path",
  edit: "path",
  multiedit: "path",
  read: "path",
  grep: "path",
  find: "path",
  ls: "path",
});

const SHELL_TOOLS = new Set(["bash", "powershell"]);
const READ_TOOLS = new Set(["read", "grep", "find", "ls"]);

/**
 * Keys forwarded for a tool this guard has no mapping for. Deliberately narrow: spreading the whole input
 * meant an unknown tool that happened to carry a `command` key was classified as a SHELL call by the
 * policy's duck-typing, instead of falling through to "no rule for tool X" (which is what makes an
 * unrecognised tool default-deny).
 */
const PASSTHROUGH_KEYS = ["path", "file_path", "pattern"];

/** Read the policy envelope. Never throws: a broken envelope must produce a blocking guard, not a crash. */
export function loadPolicy(env = process.env) {
  const raw = env.HARNESS_POLICY_JSON;
  if (!raw) throw new Error("HARNESS_POLICY_JSON is not set — the harness did not install a policy");
  const p = JSON.parse(raw);
  return {
    cwd: p.cwd || process.cwd(),
    writable_roots: p.writable_roots || [],
    readable_roots: p.readable_roots || [],
    readonly_shell: p.readonly_shell !== false,
    disabled_tools: p.disabled_tools || [],
    granted_tools: p.granted_tools || [],
    denials_path: env.HARNESS_DENIALS_PATH || "",
    denials_echo: env.HARNESS_DENIALS_ECHO === "1",
    cmd: JSON.parse(env.HARNESS_POLICY_CMD || "[]"),
  };
}

/** Shell out to `python -m harness.policy check`. Throws on anything unexpected; callers block on throw. */
export function callPolicy(request, policy) {
  const cmd = policy.cmd;
  if (!Array.isArray(cmd) || cmd.length === 0) throw new Error("HARNESS_POLICY_CMD is empty");
  const res = spawnSync(cmd[0], cmd.slice(1), {
    input: JSON.stringify(request),
    encoding: "utf8",
    timeout: 30000,
    maxBuffer: 4 * 1024 * 1024,
    // explicit cwd: spawnSync inherits the ambient one, and a process whose cwd has been deleted fails
    // ENOENT before the policy is even reached — which the guard would (correctly but uselessly) treat as
    // a reason to block. The run dir always exists for as long as the run does.
    cwd: policy.cwd,
  });
  if (res.error) throw res.error;
  if (res.status !== 0) throw new Error(`policy check exited ${res.status}: ${(res.stderr || "").slice(0, 500)}`);
  const out = (res.stdout || "").trim();
  if (!out) throw new Error("policy check produced no verdict on stdout");
  const verdict = JSON.parse(out.split("\n").pop());
  if (!verdict || typeof verdict.decision !== "string") throw new Error(`policy verdict has no decision: ${out}`);
  return verdict;
}

/**
 * The whole decision, as a pure function of (tool, input, policy) plus an injected policy caller — so the
 * node test can exercise it without a Python subprocess, and so no decision depends on sibling tool
 * results (pi preflights siblings sequentially then executes them concurrently, so cross-call state is
 * unsound by construction).
 *
 * Returns {block: false} or {block: true, reason}.
 */
export function decide({ tool, input, policy, call = callPolicy }) {
  const native = String(tool || "");
  const inp = input || {};

  if ((policy.disabled_tools || []).includes(native)) {
    return { block: true, reason: `Tool "${native}" is disabled for this stage by the harness.` };
  }
  // An MCP tool the stage granted by name (Claude: --allowedTools). The policy has no path rule for it, so
  // asking would only return "no policy rule" and default-deny the grant. A trailing "*" grants a prefix.
  if (isGranted(native, policy.granted_tools)) return { block: false };

  const canonical = CANONICAL_TOOL[native] || native;
  const request = {
    tool: canonical,
    native_tool: native,
    input: policyInput(native, inp, policy),
    cwd: policy.cwd,
    writable_roots: policy.writable_roots,
    readable_roots: policy.readable_roots,
    readonly_shell: policy.readonly_shell,
  };

  let verdict;
  try {
    verdict = call(request, policy);
  } catch (err) {
    // fail safe, deliberately: pi already blocks on a throwing handler, and a guard that cannot reach its
    // policy has no business letting a call through.
    return { block: true, reason: `harness policy check failed (blocking): ${err && err.message ? err.message : err}` };
  }

  if (verdict.decision === "allow") return { block: false };
  if (verdict.decision === "deny") return { block: true, reason: verdict.reason || "denied by harness policy" };
  // An explicit "I am not enforcing this class at all" is an allowance, not an ambiguity. The policy does
  // not send this field today; honouring it means the day it does, pi stops inventing a denial.
  if (verdict.enforced === false) return { block: false };

  // "ask" = the policy has no opinion. Headless there is nobody to ask, so it is a denial — EXCEPT for a
  // shell call when the harness itself switched the read-only classifier off (Containment.readonly_shell
  // = false, the INGEST-shaped stage): there "ask" means "the mutation deny-list passed and I was told not
  // to classify", which is the same allowance a Claude run with the approver hook off gets.
  if (SHELL_TOOLS.has(native) && policy.readonly_shell === false) return { block: false };
  // Read confinement the harness never declared is not ours to invent. With no readable_roots the policy
  // returns "not enforced" as an `ask`, and under Claude that means the engine's own --add-dir governs;
  // blocking here made a pi stage unable to read anything outside the run dir while the identical policy
  // under Claude declined to enforce. Decided from the envelope, not from the reason string.
  if (READ_TOOLS.has(native) && (policy.readable_roots || []).length === 0) return { block: false };
  return {
    block: true,
    reason:
      `${verdict.reason || "no policy opinion"} — blocked because this run is headless and has nobody to ask. ` +
      `Use a provably read-only form, or write under the run dir.`,
  };
}

/** Is `native` one of the explicitly granted MCP tools? Only `mcp__` names can be granted this way. */
export function isGranted(native, granted) {
  if (!native.startsWith("mcp__")) return false;
  return (granted || []).some((g) => (g.endsWith("*") ? native.startsWith(g.slice(0, -1)) : native === g));
}

/** Build the policy-facing input: a resolved absolute path, or the shell command, and nothing else. */
function policyInput(native, inp, policy) {
  if (SHELL_TOOLS.has(native)) return { command: String(inp.command ?? "") };
  const key = PATH_KEY[native];
  if (!key) {
    const out = {};
    for (const k of PASSTHROUGH_KEYS) if (inp[k] !== undefined) out[k] = inp[k];
    return out;
  }
  // grep/find/ls may omit the root; they then walk cwd, which is what the policy must judge.
  const raw = inp[key] ?? inp.file_path ?? (native === "grep" || native === "find" || native === "ls" ? "." : "");
  const resolved = raw ? path.resolve(policy.cwd, String(raw)) : policy.cwd;
  const out = { path: resolved };
  if (inp.pattern !== undefined) out.pattern = inp.pattern;
  return out;
}

/** Append one denial record to the side channel the translator reads. Never throws. */
export function recordDenial(record, policy) {
  const line = JSON.stringify({ type: "harness_denial", ts: Date.now() / 1000, ...record });
  if (policy.denials_path) {
    try {
      appendFileSync(policy.denials_path, line + "\n", "utf8");
    } catch {
      /* the side channel is evidence, not control flow — a failed append must not un-block a denial */
    }
  }
  // NB: in --mode json pi routes extension console output to stderr (measured), so this lands in
  // logs/<label>.pi.stderr — never in the event stream. The side file above is what the translator reads.
  if (policy.denials_echo) console.log(line);
}

export default function (pi) {
  let policy = null;
  let loadError = null;
  try {
    policy = loadPolicy();
  } catch (err) {
    loadError = err && err.message ? err.message : String(err);
  }

  pi.on("tool_call", async (event) => {
    if (loadError) {
      // No policy envelope means no containment. Block everything and say why once per call: pi 0.85.1
      // reports extension errors only on stderr in --mode json, so a thrown error here would present as
      // "tools mysteriously stopped working" with no trace in the event stream.
      return { block: true, reason: `harness guard could not load its policy (blocking all tools): ${loadError}` };
    }
    const verdict = decide({ tool: event.toolName, input: event.input, policy });
    if (!verdict.block) return undefined;
    recordDenial({ toolCallId: event.toolCallId, tool: event.toolName, reason: verdict.reason }, policy);
    return { block: true, reason: verdict.reason };
  });
}
