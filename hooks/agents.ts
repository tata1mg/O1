import type { AgentRow, AgentStatus } from '../types'

/** Lines kept for the log on show: a pane draws the first 100,000 characters of its texts. */
export const MAX_LINES = 400

const HOTKEYS = '123456789abcdefghijklmnopqrstuvwxyz'

/** `investigate.T1.run` → `investigate · T1`; `draft.lead.write` → `draft · lead (write)`. */
export function labelOf(log: string): string {
  const [stage = log, agent, ...step] = log.split('.')
  if (agent === undefined) {
    return log
  }
  const rest = step.join('.')

  return rest === '' || rest === 'run' ? `${stage} · ${agent}` : `${stage} · ${agent} (${rest})`
}

/** The text after a line's `HH:MM:SS  ` stamp. */
function body(line: string): string {
  const m = /^\d\d:\d\d:\d\d {2}(.*)$/.exec(line)

  return m?.[1] ?? line
}

export function parseLog(log: string, text: string): AgentRow {
  const lines = text.split('\n').filter(l => l.trim() !== '')
  let status: AgentStatus = 'running'
  let calls = 0
  for (const line of lines) {
    const b = body(line)
    const call = /^#(\d+) /.exec(b)
    if (call?.[1] !== undefined) {
      calls = Math.max(calls, Number(call[1]))
    } else if (b.startsWith('■ done')) {
      status = 'done'
    } else if (b.startsWith('■ FAILED')) {
      status = 'failed'
    }
  }
  const startedAt = /^(\d\d:\d\d:\d\d)/.exec(lines[0] ?? '')?.[1] ?? ''

  return { log, label: labelOf(log), status, calls, startedAt }
}

/** Agents in the order they started (a run spans hours, not midnight, in practice). */
export function ordered(rows: AgentRow[]): AgentRow[] {
  return [...rows].sort((a, b) => a.startedAt.localeCompare(b.startedAt) || a.log.localeCompare(b.log))
}

/** The picked log while it exists, else the newest running agent, else the last to start. */
export function shownOf(rows: AgentRow[], picked: string | null): string | null {
  if (picked !== null && rows.some(r => r.log === picked)) {
    return picked
  }
  const running = rows.filter(r => r.status === 'running')

  return (running[running.length - 1] ?? rows[rows.length - 1])?.log ?? null
}

export function hotkeyOf(index: number): string | undefined {
  return HOTKEYS[index]
}

export function tail(text: string): string[] {
  const lines = text.split('\n').filter(l => l !== '')

  return lines.slice(-MAX_LINES)
}

/**
 * The run folder a Bash command starts or watches: the `rfc` pipeline or a stage
 * (`--run-dir <path>`), or an agent card (`rfc_agent.watch <path> <log>`).
 * Only absolute paths: the hooks have no shell to expand `~` or `$RUN`.
 */
export function runDirOf(command: string): string | null {
  if (!/\brfc_agent\b|\brfc\b/.test(command)) {
    return null
  }
  const arg = String.raw`(?:"([^"]+)"|'([^']+)'|([^\s"';|&>]+))`
  const m =
    new RegExp(String.raw`--run-dir[ =]` + arg).exec(command) ??
    new RegExp(String.raw`rfc_agent\.watch\s+` + arg).exec(command)
  const path = m?.[1] ?? m?.[2] ?? m?.[3]

  return path !== undefined && path.startsWith('/') ? path.replace(/\/+$/, '') : null
}

export type LineKind = 'start' | 'tool' | 'message' | 'more' | 'done' | 'failed' | 'other'

/** What a tool call does, for its color: reads, writes, the context layer. */
export type ToolKind = 'read' | 'write' | 'context' | 'other'

export type Line = {
  kind: LineKind
  /** `20:56:25`, or '' for a message's later lines (written without a stamp). */
  time: string
  /** For a tool line: `#3`. */
  n: string
  /** For a tool line: the tool, MCP prefix shortened (`fleetlens get_callers`). */
  tool: string
  toolKind: ToolKind
  /** The rest: the target, the message, the status text. */
  text: string
}

const READS = new Set(['Read', 'Grep', 'Glob', 'LS'])
const WRITES = new Set(['Write', 'Edit', 'MultiEdit', 'NotebookEdit'])

/** One log line, split into the parts the pane styles. */
export function classify(raw: string): Line {
  const m = /^(\d\d:\d\d:\d\d) {2}(.*)$/.exec(raw)
  const base: Line = { kind: 'other', time: m?.[1] ?? '', n: '', tool: '', toolKind: 'other', text: m?.[2] ?? raw }
  if (m === null) {
    // a message's later lines are indented, with no stamp
    return /^\s/.test(raw) ? { ...base, kind: 'more', text: raw.trim() } : base
  }
  const b = base.text
  const call = /^(#\d+) (\S+) ?(.*)$/.exec(b)
  if (call !== null) {
    const name = call[2] ?? ''
    const mcp = /^mcp__([^_]+(?:_[^_]+)*?)__(.+)$/.exec(name)
    const toolKind: ToolKind = mcp ? 'context' : READS.has(name) ? 'read' : WRITES.has(name) ? 'write' : 'other'
    return { ...base, kind: 'tool', n: call[1] ?? '', tool: mcp ? `${mcp[1]} ${mcp[2]}` : name, toolKind, text: call[3] ?? '' }
  }
  if (b.startsWith('💬')) {
    return { ...base, kind: 'message', text: b.replace(/^💬\s*/, '') }
  }
  if (b.startsWith('▶')) {
    return { ...base, kind: 'start', text: b.replace(/^▶\s*/, '') }
  }
  if (b.startsWith('■ done')) {
    return { ...base, kind: 'done', text: b.replace(/^■\s*/, '') }
  }
  if (b.startsWith('■ FAILED')) {
    return { ...base, kind: 'failed', text: b.replace(/^■\s*/, '') }
  }

  return base
}
