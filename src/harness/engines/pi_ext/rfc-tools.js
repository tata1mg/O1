/**
 * rfc-tools — the rfc-agent pieces a Pi stage needs beyond containment, loaded next to harness-guard.js.
 *
 * Two jobs, both configured by ONE JSON file the Python side writes (0600, it may carry an MCP token) and
 * names in RFC_TOOLS_CONFIG:
 *
 *   {"mcp":  {"<server>": {"url": "...", "headers": {...}}},          // registered for THIS session only
 *    "hook": {"cmd": ["/abs/python", "/abs/hook.py"]},                 // the Claude hook script, reused
 *    "guards": [{"prefix": "mcp__playwright__", "cmd": [...]}],        // a PreToolUse script per tool prefix
 *    "write_checks": [{"path": "/abs/verdict.json", "cmd": [...]}],    // run after a write to that file
 *    "threshold_compaction": false}                                    // cancel Axon's early compaction
 *
 * 1. MCP. `pi.registerMcpServer()` adds the server for this session without touching the operator's
 *    mcp.json (under Axon that is ~/.axon/pi/mcp.json, shared with their interactive use). Exposure is
 *    `direct`, so the model sees `mcp__<server>__<tool>` as plain tools — the exact names the Claude prompts
 *    already use — instead of having to discover them through codemode.
 *
 * 2. The Investigate hook. On Claude it is a PreToolUse/PostToolUse command hook (rfc_agent/investigate/
 *    hook.py). Here the same script runs from `tool_call` (pre: may this file be written?) and `tool_result`
 *    (post: citation check + call budget, appended to the result the model reads). The script stays the only
 *    implementation; this file only translates Pi's event shape into the Claude hook payload. Its INVESTIGATE_*
 *    environment is inherited from the Pi process, which the Python side sets exactly as it does for Claude.
 *
 * 3. Tool guards. A stage that grants tools which ACT (the browser of execute_ui_agent, the device of
 *    execute_app) holds every call with a script: on Claude a PreToolUse command hook (rfc_agent/hooks/
 *    guard_browser.py, guard_device.py). Here the same script runs from `tool_call` for every tool whose name
 *    starts with the guard's prefix, with the Claude payload on stdin: exit 0 allows, exit 2 blocks with the
 *    script's stderr, and anything else blocks too. Its limits live in environment variables and a count file,
 *    both inherited from the Pi process.
 *
 * 4. Write checks. After a write to a named file, a command runs and what it prints is appended to the result
 *    the model reads — the verdict check an agent runs through Bash on Claude, with no shell here.
 *
 * Failure policy mirrors the guard: a pre check that cannot run BLOCKS the write; a post check that cannot run
 * adds nothing (the Claude hook never blocks on post either).
 */

import { spawnSync } from "node:child_process";
import { appendFileSync, readFileSync } from "node:fs";
import path from "node:path";

/** Pi built-in tool -> the Claude tool name hook.py routes on. */
export const CLAUDE_NAME = Object.freeze({
  read: "Read",
  write: "Write",
  edit: "Edit",
  multiedit: "MultiEdit",
  grep: "Grep",
  find: "Glob",
  ls: "LS",
  bash: "Bash",
});

const WRITE_TOOLS = new Set(["write", "edit", "multiedit"]);

export function loadConfig(env = process.env) {
  const p = env.RFC_TOOLS_CONFIG;
  if (!p) return { mcp: {}, hook: null, guards: [], write_checks: [], threshold_compaction: true };
  const raw = JSON.parse(readFileSync(p, "utf8"));
  const cmds = (list, key) => (Array.isArray(list) ? list : []).filter(
    (x) => x && typeof x[key] === "string" && x[key] && Array.isArray(x.cmd) && x.cmd.length);
  return {
    mcp: raw.mcp || {},
    hook: raw.hook && Array.isArray(raw.hook.cmd) && raw.hook.cmd.length ? raw.hook : null,
    guards: cmds(raw.guards, "prefix"),
    write_checks: cmds(raw.write_checks, "path"),
    threshold_compaction: raw.threshold_compaction !== false,
  };
}

/** Pi tool input -> the Claude-shaped `tool_input` hook.py reads (absolute file_path, pattern, path). */
export function claudeInput(toolName, input, cwd) {
  const inp = input || {};
  const out = {};
  const p = inp.path ?? inp.file_path;
  if (p !== undefined && p !== null && p !== "") {
    const abs = path.resolve(cwd, String(p));
    if (WRITE_TOOLS.has(toolName) || toolName === "read") out.file_path = abs;
    else out.path = abs;
  }
  if (inp.pattern !== undefined) out.pattern = inp.pattern;
  if (inp.content !== undefined) out.content = inp.content;
  return out;
}

export function runHook(hook, phase, payload, cwd) {
  const cmd = hook.cmd;
  return spawnSync(cmd[0], [...cmd.slice(1), phase], {
    input: JSON.stringify(payload),
    encoding: "utf8",
    timeout: 60000,
    maxBuffer: 4 * 1024 * 1024,
    cwd,
  });
}

/** Pre: {block:false} or {block:true, reason}. Exit 0 allows, exit 2 is hook.py's deny, anything else fails safe. */
export function preDecision(hook, toolName, input, cwd, run = runHook) {
  if (!hook || !WRITE_TOOLS.has(toolName)) return { block: false };
  const payload = { hook_event_name: "PreToolUse", tool_name: CLAUDE_NAME[toolName] || toolName,
                    tool_input: claudeInput(toolName, input, cwd) };
  let res;
  try {
    res = run(hook, "pre", payload, cwd);
  } catch (err) {
    return { block: true, reason: `write check failed (blocking): ${err && err.message ? err.message : err}` };
  }
  if (res.error) return { block: true, reason: `write check failed (blocking): ${res.error.message || res.error}` };
  if (res.status === 0) return { block: false };
  const why = (res.stderr || "").trim() || `write check exited ${res.status}`;
  return { block: true, reason: why };
}

/** Post: the extra text to append to the tool result ("" = nothing). Never throws. */
export function postFeedback(hook, toolName, input, isError, cwd, run = runHook) {
  if (!hook) return "";
  const payload = { hook_event_name: "PostToolUse", tool_name: CLAUDE_NAME[toolName] || toolName,
                    tool_input: claudeInput(toolName, input, cwd), tool_response: { is_error: !!isError } };
  try {
    const res = run(hook, "post", payload, cwd);
    if (res.error || res.status !== 0) return "";
    const out = (res.stdout || "").trim();
    if (!out) return "";
    const parsed = JSON.parse(out.split("\n").pop());
    const ctx = parsed && parsed.hookSpecificOutput && parsed.hookSpecificOutput.additionalContext;
    return typeof ctx === "string" ? ctx : "";
  } catch {
    return "";
  }
}

function runCmd(cmd, payload, cwd) {
  return spawnSync(cmd[0], cmd.slice(1), {
    input: payload === undefined ? "" : JSON.stringify(payload),
    encoding: "utf8",
    timeout: 60000,
    maxBuffer: 4 * 1024 * 1024,
    cwd,
  });
}

/**
 * The stage's tool guards: {block:false} or {block:true, reason}. Every guard whose prefix the tool name
 * starts with is asked, with the tool's own name and input (an MCP tool's are already the Claude ones).
 * FAILS CLOSED: a guard that cannot run, or exits with anything but 0, blocks the call.
 */
export function guardDecision(guards, toolName, input, cwd, run = runCmd) {
  const name = String(toolName || "");
  for (const g of guards || []) {
    if (!name.startsWith(g.prefix)) continue;
    const payload = { hook_event_name: "PreToolUse", tool_name: name, tool_input: input || {} };
    let res;
    try {
      res = run(g.cmd, payload, cwd);
    } catch (err) {
      return { block: true, reason: `tool guard failed (blocking): ${err && err.message ? err.message : err}` };
    }
    if (res.error) return { block: true, reason: `tool guard failed (blocking): ${res.error.message || res.error}` };
    if (res.status === 0) continue;
    const why = (res.stderr || "").trim();
    return { block: true, reason: res.status === 2 && why ? why : `tool guard exited ${res.status} (blocking): ${why}` };
  }
  return { block: false };
}

/** The text to append to a write's result: what the file's check printed ("" = no check, or it said nothing). */
export function writeCheck(checks, toolName, input, isError, cwd, run = runCmd) {
  if (isError || !WRITE_TOOLS.has(toolName)) return "";
  const p = (input || {}).path ?? (input || {}).file_path;
  if (p === undefined || p === null || p === "") return "";
  const abs = path.resolve(cwd, String(p));
  const out = [];
  for (const c of checks || []) {
    if (path.resolve(c.path) !== abs) continue;
    try {
      const res = run(c.cmd, undefined, cwd);
      if (res.error) continue;
      const text = `${res.stdout || ""}${res.stderr || ""}`.trim();
      if (text) out.push(text);
    } catch {
      /* a check that cannot run adds nothing: the stage checks the file again itself */
    }
  }
  return out.join("\n");
}

/** One denial into the harness's side file, in the guard's own shape, so the trace shows it. Never throws. */
function recordDenial(event, reason) {
  const dest = process.env.HARNESS_DENIALS_PATH;
  if (!dest) return;
  try {
    appendFileSync(dest, JSON.stringify({ type: "harness_denial", ts: Date.now() / 1000,
      toolCallId: event.toolCallId, tool: event.toolName, reason }) + "\n", "utf8");
  } catch {
    /* evidence, not control flow */
  }
}

export default function (pi) {
  let cfg = null;
  let loadError = null;
  try {
    cfg = loadConfig();
  } catch (err) {
    loadError = err && err.message ? err.message : String(err);
  }

  if (cfg && cfg.threshold_compaction === false) {
    // Axon turns on threshold compaction (with its own summary model) for every session. Compaction is
    // lossy, and a stage that summarises its evidence away makes the citation gates lie, so a pipeline stage
    // cancels the THRESHOLD kind. Overflow recovery is left alone: there the turn would otherwise just fail.
    pi.on("session_before_compact", async (event) => (event.reason === "threshold" ? { cancel: true } : undefined));
  }

  if (cfg) {
    for (const [name, spec] of Object.entries(cfg.mcp)) {
      const { type: _type, ...rest } = spec || {};
      pi.registerMcpServer(name, { ...rest, exposure: "direct" });
    }
  }

  pi.on("tool_call", async (event, ctx) => {
    if (loadError) {
      // the config names the write allow-list; without it no write may be trusted
      if (WRITE_TOOLS.has(event.toolName)) {
        return { block: true, reason: `rfc-tools could not load its config (blocking writes): ${loadError}` };
      }
      // the config also names the guards an MCP tool is held by; without it none of them may run
      if (String(event.toolName || "").startsWith("mcp__")) {
        return { block: true, reason: `rfc-tools could not load its config (blocking MCP tools): ${loadError}` };
      }
      return undefined;
    }
    const cwd = (ctx && ctx.cwd) || process.cwd();
    const verdict = preDecision(cfg.hook, event.toolName, event.input, cwd);
    if (verdict.block) return { block: true, reason: verdict.reason };
    const guarded = guardDecision(cfg.guards, event.toolName, event.input, cwd);
    if (!guarded.block) return undefined;
    recordDenial(event, guarded.reason);
    return { block: true, reason: guarded.reason };
  });

  pi.on("tool_result", async (event, ctx) => {
    if (!cfg) return undefined;
    const cwd = (ctx && ctx.cwd) || process.cwd();
    const extra = [postFeedback(cfg.hook, event.toolName, event.input, event.isError, cwd),
                   writeCheck(cfg.write_checks, event.toolName, event.input, event.isError, cwd)]
      .filter(Boolean).join("\n\n");
    if (!extra) return undefined;
    return { content: [...(event.content || []), { type: "text", text: `\n\n${extra}` }] };
  });
}
