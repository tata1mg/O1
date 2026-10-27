/**
 * rfc-tools — the rfc-agent pieces a Pi stage needs beyond containment, loaded next to harness-guard.js.
 *
 * Two jobs, both configured by ONE JSON file the Python side writes (0600, it may carry an MCP token) and
 * names in RFC_TOOLS_CONFIG:
 *
 *   {"mcp":  {"<server>": {"url": "...", "headers": {...}}},          // registered for THIS session only
 *    "hook": {"cmd": ["/abs/python", "/abs/hook.py"]},                 // the Claude hook script, reused
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
 * Failure policy mirrors the guard: a pre check that cannot run BLOCKS the write; a post check that cannot run
 * adds nothing (the Claude hook never blocks on post either).
 */

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
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
  if (!p) return { mcp: {}, hook: null, threshold_compaction: true };
  const raw = JSON.parse(readFileSync(p, "utf8"));
  return {
    mcp: raw.mcp || {},
    hook: raw.hook && Array.isArray(raw.hook.cmd) && raw.hook.cmd.length ? raw.hook : null,
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
      return undefined;
    }
    const cwd = (ctx && ctx.cwd) || process.cwd();
    const verdict = preDecision(cfg.hook, event.toolName, event.input, cwd);
    return verdict.block ? { block: true, reason: verdict.reason } : undefined;
  });

  pi.on("tool_result", async (event, ctx) => {
    if (!cfg || !cfg.hook) return undefined;
    const cwd = (ctx && ctx.cwd) || process.cwd();
    const extra = postFeedback(cfg.hook, event.toolName, event.input, event.isError, cwd);
    if (!extra) return undefined;
    return { content: [...(event.content || []), { type: "text", text: `\n\n${extra}` }] };
  });
}
