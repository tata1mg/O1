# Install rfc-agent and test-agent

Two tools from the same repo, for a Mac:

- **rfc-agent** turns a PRD into a tech RFC.
- **test-agent** writes QA test cases into Kiwi and runs the confirmed ones on the web and on Android. It runs on
  rfc-agent's code and settings, so both are always installed together.

They run in **Claude Code** or in **Axon** (the org's managed Pi). Each has its own install guide.

**To install, open the tool you work in and say one of:**

> Set up rfc-agent by following INSTALL.md

> Set up test-agent by following INSTALL.md

> Set up rfc-agent and test-agent by following INSTALL.md

(Give the path or the GitHub link to this file.) The agent works out which tool it is running in and follows the
right guide. You can also open the guide yourself:

| You use | Install guide | Commands you will use |
|---|---|---|
| **Claude Code** | [CLAUDE_INSTALL.md](CLAUDE_INSTALL.md) | `/rfc-agent:rfc`, `/test-agent:test`, `/test-agent:execute` |
| **Axon** | [AXON_INSTALL.md](AXON_INSTALL.md) | `/rfc`, `/test` |

Every setting in the config file: [CONFIG.md](CONFIG.md).

---

## Instructions for the agent

You are an AI agent that has been asked to set up rfc-agent, test-agent, or both. Before doing anything else,
work out which harness (the program you are running inside) you are in, then follow exactly one guide.

### 1. Detect the harness

Decide from what you know about yourself first. Your system prompt and your tools say which program you run in.
Then confirm with one check, using single commands with no shell syntax (no `$VAR`, `&&`, pipes or redirects):

| Harness | You are in it if | Confirm with | Follow |
|---|---|---|---|
| **Claude Code** | Your system prompt says you are Claude Code, or the Claude Agent SDK | `printenv CLAUDECODE` prints `1` | [CLAUDE_INSTALL.md](CLAUDE_INSTALL.md) |
| **Axon** (Pi) | Your system prompt says you are Axon or Pi, you were started with `axon`, or this guide was opened from `~/.axon/pi/git/` | `printenv PI_CODING_AGENT_DIR` prints a path, or the folder `~/.axon` exists | [AXON_INSTALL.md](AXON_INSTALL.md) |

Rules:

- What you know about yourself wins over the environment check. The check is only there to catch a mistake. If the
  two disagree, say so to the user and ask which one they are using (a single choice: Claude Code or Axon).
- The environment check can be misleading when one harness is started from inside another (for example `axon`
  launched from a Claude Code terminal). Trust the harness you are actually running in.
- If you are in both, or cannot tell, ask the user. Never guess.

### 2. Follow the one guide

Once you know, tell the user in one line which harness you detected and which guide you are following, then open
that guide and follow it from its own "Instructions" section. Do not follow the other one.

Both guides assume the harness itself is already installed and signed in, and that you are running inside it. If
you are in Axon and it is not signed in, tell the user to sign in first (their team's pilot guide covers it) and
stop; do not try to install Axon or Claude Code for them.

### 3. Any other harness: stop

If you are running in anything else (Codex, Cursor, Gemini CLI, Copilot, Windsurf, a plain terminal, or anything
you cannot name), **stop here. Do not install anything, change any file, or run any setup command.** Tell the
user, naming the harness you are in:

> **<harness name> is not supported by rfc-agent and test-agent yet.** They run in Claude Code or Axon only.
> Please raise a GitHub issue at https://github.com/tata1mg/O1/issues asking for support for <harness name>, and
> the team will look into it. In the meantime you can install in Claude Code or Axon using this same guide.

If you cannot tell what harness you are in, say that instead and ask the user.
