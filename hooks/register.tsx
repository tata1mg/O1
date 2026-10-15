// The rfc agents pane: every agent of the run this session started, its state and call count, and the
// picked agent's own log (logs/agents/<stage>.<agent>.<step>.log, written by investigate/progress.py).
// Drawn as text in the terminal; nothing listens on a port. Opens when a Bash command starts a run
// (`--run-dir <path>`) or with /rfc-agents <run folder>.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { AgentRow, AgentStatus, Snapshot } from '../types'
import { classify, hotkeyOf, ordered, parseLog, runDirOf, shownOf, tail } from './agents'
import type { ToolKind } from './agents'

const PANE = 'rfc-agents'
const COLUMNS = 100 // the dock's width when it opens; the person's drag wins

const runDir = atom({ plugin: 'rfc-agent', key: 'runDir' } as const, null)
const picked = atom({ plugin: 'rfc-agent', key: 'picked' } as const, null)
const snapshot = atom({ plugin: 'rfc-agent', key: 'snapshot' } as const, null)

const STATUS_COLOR: Record<AgentStatus, string> = { running: 'suggestion', done: 'success', failed: 'error' }
// theme keys, so the colors follow the person's light or dark theme
const TOOL_COLOR: Record<ToolKind, string> = { read: 'suggestion', write: 'warning', context: 'claude', other: 'text' }

// per log: what it was parsed from, so an unchanged file is not read again
const cache = new Map<string, { mtimeMs: number; size: number; row: AgentRow; text: string }>()
let lastKey = ''
let lastShown: string | null = null
let isPolling = false

async function poll($: EngineInterface): Promise<void> {
  const dir = await read($, runDir)
  if (dir === null || isPolling) {
    return
  }
  isPolling = true
  try {
    const entries = await $.fs.list(`${dir}/logs/agents`).catch(() => [])
    for (const entry of entries) {
      if (entry.kind !== 'file' || !entry.name.endsWith('.log')) {
        continue
      }
      const log = entry.name.slice(0, -'.log'.length)
      const seen = cache.get(log)
      if (seen?.mtimeMs === entry.mtimeMs && seen.size === entry.size) {
        continue
      }
      const text = await $.fs.read(`${dir}/logs/agents/${entry.name}`).catch(() => '')
      cache.set(log, { mtimeMs: entry.mtimeMs, size: entry.size, row: parseLog(log, text), text })
    }
    const agents = ordered([...cache.values()].map(c => c.row))
    const shown = shownOf(agents, await read($, picked))
    const lines = shown === null ? [] : tail(cache.get(shown)?.text ?? '')
    const key = JSON.stringify([dir, agents, shown, lines.length, lines[lines.length - 1]])
    if (key !== lastKey) {
      lastKey = key
      const next: Snapshot = { runDir: dir, agents, shown, lines }
      await update($, snapshot, () => next)
      if (shown !== lastShown) {
        lastShown = shown
        void $.ui.scroll({ in: PANE, to: 'end' }).catch(() => undefined)
      }
    }
  } finally {
    isPolling = false
  }
}

async function follow($: EngineInterface, dir: string): Promise<void> {
  if ((await read($, runDir)) !== dir) {
    cache.clear()
    lastKey = ''
    lastShown = null
    await update($, runDir, () => dir)
    await update($, picked, () => null)
    await update($, snapshot, () => null)
  }
  await $.ui.open({ id: PANE, title: 'rfc agents', columns: COLUMNS })
  await poll($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'rfc-agents',
      description: 'Show the agents of an rfc-agent run in a pane (give a run folder, or reopen the last)',
      argumentHint: '[/absolute/path/to/run-folder]',
    })
    $.clock.every(1000, () => void poll($))

    return next(e)
  })

  // the run starts (or a stage, or an agent card) from a Bash command: follow its folder
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const dir = runDirOf(String(e.command ?? ''))
    if (dir !== null) {
      void follow($, dir).catch(() => undefined)
    }

    return next(e)
  }).catch(($, e, next) => next(e)) // a fault here never blocks the person's Bash call

  on('command.run', { command: 'rfc-agents' }, async ($, e) => {
    const arg = e.args.trim().replace(/^["']|["']$/g, '').replace(/\/+$/, '')
    const dir = arg !== '' ? arg : await read($, runDir)
    if (dir === null) {
      return { text: 'No run yet. Give its folder: /rfc-agents /absolute/path/to/run-folder' }
    }
    if (!dir.startsWith('/')) {
      return { text: `Give the run folder as an absolute path (got ${dir}).` }
    }
    const isRun = await $.fs.stat(`${dir}/logs`).then(st => st.kind === 'dir').catch(() => false)
    if (!isRun) {
      return { text: `No logs/ folder in ${dir} — is that an rfc-agent run folder?` }
    }
    await follow($, dir)

    return { text: `Showing the agents of ${dir}.` }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const snap = await read($, snapshot)
    const width = Math.max(20, e.props.bodyColumns)

    if (snap === null) {
      return <Text dimColor>Waiting for an rfc-agent run. Start one, or /rfc-agents /path/to/run-folder</Text>
    }

    const isFollowing = (await read($, picked)) === null
    const count = (s: AgentStatus) => snap.agents.filter(a => a.status === s).length
    const shownRow = snap.agents.find(a => a.log === snap.shown)
    const folder = snap.runDir.split('/').pop() ?? snap.runDir

    return (
      <Box flexDirection="column">
        <Text dimColor wrap="truncate-end">
          {folder} · {count('running')} running · {count('done')} done
          {count('failed') > 0 ? ` · ${count('failed')} failed` : ''}
        </Text>
        {snap.agents.length === 0 && <Text dimColor>No agent has started yet (Scope is still fetching the PRD).</Text>}
        {snap.agents.map((a, i) => (
          <Button
            key={`pick:${a.log}`}
            hotkey={hotkeyOf(i)}
            plain
            dimColor={a.log !== snap.shown}
            onPress={() => void update($, picked, () => a.log).then(() => poll($))}
          >
            {a.log === snap.shown ? '▸ ' : '  '}
            {a.label} <Text color={STATUS_COLOR[a.status]}>{a.status}</Text>{' '}
            <Text dimColor>{a.calls} calls</Text>
          </Button>
        ))}
        {!isFollowing && (
          <Button key="follow" hotkey="0" plain dimColor onPress={() => void update($, picked, () => null).then(() => poll($))}>
            follow the newest running agent
          </Button>
        )}
        <Text dimColor>{'─'.repeat(width)}</Text>
        {shownRow !== undefined && (
          <Text bold wrap="truncate-end">
            {shownRow.label}
            <Text dimColor>{isFollowing ? '  (following the newest running agent)' : ''}</Text>
          </Text>
        )}
        {snap.lines.map(raw => {
          const line = classify(raw)
          const time = <Text dimColor>{line.time === '' ? '         ' : line.time} </Text>
          switch (line.kind) {
            case 'tool':
              return (
                <Text wrap="wrap">
                  {time}
                  <Text dimColor>{line.n} </Text>
                  <Text bold color={TOOL_COLOR[line.toolKind]}>{line.tool}</Text> {line.text}
                </Text>
              )
            case 'message':
            case 'more':
              return (
                <Text wrap="wrap">
                  {time}
                  <Text color="claude">{line.kind === 'message' ? '› ' : '  '}</Text>
                  <Text italic>{line.text}</Text>
                </Text>
              )
            case 'start':
              return <Text wrap="wrap">{time}<Text bold color="suggestion">▶ {line.text}</Text></Text>
            case 'done':
              return <Text wrap="wrap">{time}<Text bold color="success">■ {line.text}</Text></Text>
            case 'failed':
              return <Text wrap="wrap">{time}<Text bold color="error">■ {line.text}</Text></Text>
            default:
              return <Text wrap="wrap">{time}{line.text}</Text>
          }
        })}
      </Box>
    )
  })
}
