// The rfc agents pane (hooks/): log parsing, the run folder a Bash command names, and the drawing.
// Run with: claude plugin test .
import type { On } from 'claude-code'
import { describe, expect, test } from 'claude-code/testing'

import { classify, labelOf, parseLog, runDirOf, shownOf, tail } from '../hooks/agents'

const T1 = `20:56:20  ▶ investigate · T1 started · model claude-sonnet-5 · session s-1
20:56:22  💬 Tracing the refund path.
20:56:25  #1 Grep 'refund' in _snap/order
20:56:31  #2 Read _snap/order/refunds/handler.py
`

describe('logs', () => {
  test('a log reads as its agent: label, state, calls, start', async () => {
    expect(parseLog('investigate.T1.run', T1)).toEqual({
      log: 'investigate.T1.run', label: 'investigate · T1', status: 'running', calls: 2, startedAt: '20:56:20',
    })
    const done = parseLog('draft.lead.write', T1 + '20:58:00  ■ done · 2 calls, 100s · open it: …\n')
    expect(done.status).toBe('done')
    expect(done.label).toBe('draft · lead (write)')
    expect(parseLog('review.R-X.run', '20:59:00  ■ FAILED: timed out after 900s · 0 calls, 900s\n').status).toBe('failed')
    expect(labelOf('scope.discovery')).toBe('scope · discovery')
    expect(labelOf('investigate.T2.run.2')).toBe('investigate · T2 (run.2)')
  })

  test('the picked agent is shown; else the newest running one', async () => {
    const rows = [
      parseLog('investigate.lead.run', '20:56:11  ▶ x\n20:57:00  ■ done\n'),
      parseLog('investigate.T1.run', '20:56:20  ▶ x\n'),
      parseLog('investigate.T2.run', '20:56:21  ▶ x\n'),
    ]
    expect(shownOf(rows, null)).toBe('investigate.T2.run')
    expect(shownOf(rows, 'investigate.lead.run')).toBe('investigate.lead.run')
    expect(shownOf(rows, 'gone.agent.run')).toBe('investigate.T2.run')
    expect(shownOf([], null)).toBe(null)
    expect(tail('a\nb\n\nc\n')).toEqual(['a', 'b', 'c'])
  })

  test('each line is split into the parts the pane styles', async () => {
    expect(classify("20:56:25  #1 Grep 'refund' in _snap/order")).toMatchObject({
      kind: 'tool', time: '20:56:25', n: '#1', tool: 'Grep', toolKind: 'read', text: "'refund' in _snap/order",
    })
    expect(classify('20:57:57  #24 Write work/reqs.json')).toMatchObject({ toolKind: 'write', text: 'work/reqs.json' })
    expect(classify('20:58:00  #3 mcp__fleetlens__get_callers symbol=apply_coupon')).toMatchObject({
      tool: 'fleetlens get_callers', toolKind: 'context', text: 'symbol=apply_coupon',
    })
    expect(classify('20:58:01  #4 mcp__claude_ai_Atlassian__getJiraIssue issueIdOrKey=TASK-1').tool)
      .toBe('claude_ai_Atlassian getJiraIssue')
    expect(classify('20:56:22  💬 Tracing the refund path.')).toMatchObject({ kind: 'message', text: 'Tracing the refund path.' })
    expect(classify('   then the coupons.')).toMatchObject({ kind: 'more', time: '', text: 'then the coupons.' })
    expect(classify('20:56:20  ▶ investigate · T1 started').kind).toBe('start')
    expect(classify('20:58:00  ■ done · 2 calls, 100s').kind).toBe('done')
    expect(classify('20:58:00  ■ FAILED: timed out after 900s').kind).toBe('failed')
    expect(classify('something else').kind).toBe('other')
  })

  test('the run folder comes from the command that starts or watches a run', async () => {
    expect(runDirOf('TASK="rfc pipeline · x" uv run --project "/p" rfc --prd 1 --run-dir "/runs/prd-1" --confirm-file > "/runs/prd-1/logs/run.out" 2>&1'))
      .toBe('/runs/prd-1')
    expect(runDirOf("uv run python -m rfc_agent.investigate --run-dir '/runs/a b/' --confirm-file")).toBe('/runs/a b')
    expect(runDirOf('TASK="investigate · T1" uv run --project /p python -m rfc_agent.watch /runs/prd-1 investigate.T1.run'))
      .toBe('/runs/prd-1')
    expect(runDirOf('uv run rfc --run-dir "$RUN"')).toBe(null) // no shell here to expand it
    expect(runDirOf('ls --run-dir /x')).toBe(null) // not an rfc-agent command
  })
})

const RUN = '/runs/prd-5654544463'
const LEAD = '20:56:11  ▶ investigate · lead started\n20:56:30  #24 Write work/reqs.json\n20:57:00  ■ done · 24 calls, 49s\n'
const FILES: Record<string, string> = {
  [`${RUN}/logs/agents/investigate.lead.run.log`]: LEAD,
  [`${RUN}/logs/agents/investigate.T1.run.log`]: T1,
}
const PANE = {
  title: 'rfc agents', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {},
} as const

// The person typing /rfc-agents <args> at the terminal.
function typed(args: string) {
  return {
    command: 'rfc-agents', args, origin: { kind: 'composer' as const },
    presentation: { isFullscreen: true, columns: 200 },
  }
}

// The test stands in for the file system and the terminal beneath the plugin.
function host(on: On, files: Record<string, string>): void {
  on('fs.stat', ($, e) => {
    if (e.path !== `${RUN}/logs`) {
      throw new Error(`ENOENT: ${e.path}`)
    }

    return { value: { kind: 'dir' as const, size: 0, mtimeMs: 0, isLink: false } }
  })
  on('fs.list', ($, e) => ({
    value: e.path === `${RUN}/logs/agents`
      ? Object.keys(files).map(f => ({
          name: f.split('/').pop() ?? f, kind: 'file' as const, size: files[f]?.length ?? 0, mtimeMs: 1, isLink: false,
        }))
      : [],
  }))
  on('fs.read', ($, e) => ({ value: files[e.path] ?? '' }))
  on('ui.open', () => ({ value: { isPlaced: true as const } }))
  on('ui.scroll', () => ({}))
}

test('/rfc-agents follows a run: the pane lists its agents, shows one, and a key picks another', async ($, on) => {
  host(on, FILES)
  const ran = await $.command.run(typed(RUN))
  expect(ran.text).toContain(RUN)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'rfc-agent', surface, component: 'Pane', requestId: 'rfc-agents', props: PANE })
    expect(await ui.find({ type: 'Text', text: /prd-5654544463 · 1 running · 1 done/ })).toBeDefined()
    expect(await ui.find({ key: 'pick:investigate.lead.run' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /#2 Read _snap\/order\/refunds\/handler.py/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /following the newest running agent/ })).toBeDefined()

    await ui.press({ key: 'pick:investigate.lead.run' })
    expect(await ui.find({ type: 'Text', text: /#24 Write work\/reqs.json/ })).toBeDefined()
    expect(await ui.find({ key: 'follow' })).toBeDefined()
    await ui.press({ key: 'follow' })
    await ui.unmount()
  }
})

test('/rfc-agents refuses a folder that is not a run', async ($, on) => {
  host(on, {})
  expect((await $.command.run(typed('/tmp/nothing'))).text).toContain('No logs/ folder')
  expect((await $.command.run(typed('relative/dir'))).text).toContain('absolute path')
})

test('before a run, the pane says how to start one', async ($, on) => {
  host(on, {})
  const ui = await $.ui.mount({ plugin: 'rfc-agent', surface: 'terminal', component: 'Pane', requestId: 'rfc-agents', props: PANE })
  expect(await ui.find({ type: 'Text', text: /Waiting for an rfc-agent run/ })).toBeDefined()
  await ui.unmount()
})

test('a Bash command that starts a run opens the pane on that run, and the command still runs', async ($, on) => {
  host(on, FILES)
  let ran = ''
  on('tool.call', ($, e) => {
    ran = String((e as { command?: string }).command ?? '')

    return { result: { stdout: 'started', stderr: '', interrupted: false } }
  })
  const command = `TASK="rfc pipeline · prd-5654544463" uv run --project /p rfc --prd 1 --run-dir "${RUN}" --confirm-file`
  await $.tool.call({ tool: 'Bash', command, run_in_background: true })
  expect(ran).toBe(command)

  const ui = await $.ui.mount({ plugin: 'rfc-agent', surface: 'terminal', component: 'Pane', requestId: 'rfc-agents', props: PANE })
  expect(await ui.find({ type: 'Text', text: /prd-5654544463 · 1 running · 1 done/ })).toBeDefined()
  await ui.unmount()
})
