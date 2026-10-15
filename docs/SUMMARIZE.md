# Summarize

Summarize writes **`out/rfc.readable.md`**: a concise, plain-language copy of the final RFC (`out/rfc.md`,
after review and fixes) for the people who decide on it and build it. The full RFC stays the source of truth,
with every citation, the full contract and every CHANGE block; the readable copy is the one people read first.

**Not reviewed, not gated.** The only check is that the file was written: one retry, then a clear error.

## Run it

Runs automatically at the end of Revise (`--no-readable` skips it), or on its own:

```
/rfc-agent:summarize --run-dir <run dir>
uv run python -m rfc_agent.summarize --run-dir <run dir> [--fresh]
```

If `out/rfc.md` hasn't changed since the last copy (content hash), nothing runs. When it changes (for example
after a Revise rerun), the copy is rewritten.

## Who writes it

A **fresh agent** — Sonnet 5.5, medium effort by default — that reads **only `out/rfc.md`**. It is not the
lead, whose session carries every investigation, so the copy cannot pick up anything the RFC doesn't say; and
it is cheaper than resuming the lead's 1M-token session. Tools: `Read`, `Write`, `Edit`; no code access, no
MCP; it may write only `out/rfc.readable.md`.

## What it contains

```
# <feature> — summary
Status · Repos · Full RFC: out/rfc.md
> TL;DR — 2–3 sentences
## What changes          3–6 bullets
## Decision              the chosen option, 2–3 reasons; each alternative in one line (why not)
## How it works          per flow: its sequence diagram (unchanged) + 3–6 numbered steps in plain words
## API changes           table: endpoint · what changes · who is affected
## Build plan            table: # · repo · what · depends on — one row per CHANGE, same order
## Rollout and risks     short bullets
## Open questions        every one from the full RFC
## ⚠ Unresolved review findings   only if the full RFC has them — every row kept
```

**Rules** (in the prompt, [readable.md](../src/rfc_agent/summarize/prompts/readable.md)): same decisions,
nothing new, no claim stronger or weaker; no code citations, file paths or line numbers (they stay in the full
RFC); names of endpoints, services and fields kept; plain words, short sentences, tables over prose; never drop
an open question, a risk or an unresolved finding to make it shorter. Target 6–12 KB.

## Configuration

`SUMMARIZE` in `~/.config/rfc-agent/config.json`:

| Key | Default |
|---|---|
| `MODEL` | `claude-sonnet-5-5` |
| `EFFORT` | `medium` |
| `TIMEOUT_S` | `600` |
| `SIZE_TARGET_KB` | `"6–12"` |

## Output

| Path | Contents |
|---|---|
| `out/rfc.readable.md` | The readable copy |
| `logs/summarize.json` | Time, cost, size vs. the full RFC |

Code: `src/rfc_agent/summarize/` (`cli.py`, `prompts/readable.md`). Tests: `tests/rfc/test_summarize.py`.
