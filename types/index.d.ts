export type AgentStatus = 'running' | 'done' | 'failed'

export type AgentRow = {
  /** The log's name under logs/agents/, without `.log`: `investigate.T1.run`. */
  log: string
  /** `investigate · T1`, the step added when it is not `run`. */
  label: string
  status: AgentStatus
  calls: number
  /** The first line's clock time (`20:56:11`), the order agents started in. */
  startedAt: string
}

export type Snapshot = {
  runDir: string
  agents: AgentRow[]
  /** The log on show: the one picked, else the newest running agent's. */
  shown: string | null
  /** Its last lines. */
  lines: string[]
}

declare module 'claude-code' {
  interface PluginState {
    'rfc-agent': {
      runDir: string | null
      /** The log the person picked; null follows the newest running agent. */
      picked: string | null
      snapshot: Snapshot | null
    }
  }
}
