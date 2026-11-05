"""Fetch Confluence/Jira through Axon's own Atlassian MCP sign-in, byte-exact — the Axon twin of
`atlassian.ConnectorSession`, used when Scope runs on Axon (`--engine axon`).

Same contract as the Claude session: a model only CALLS the tools, and every raw/ file is written by Python
from the tool result, never re-typed by a model. `scope/cli.py` cannot tell the two sessions apart.

How the Axon side differs (probed 2026-10-29, Axon 0.2.6 / Pi 1.4.0):

- The server is the operator's user-level `atlassian` entry in ~/.axon/pi/mcp.json, signed in once with
  `axon pi mcp login atlassian` (Pi keeps the OAuth token). Its default exposure is `codemode`, which hides the
  tools behind scripts. Each turn therefore runs in its own folder whose project `.pi/mcp.json` overrides ONLY
  the exposure to `direct` (Pi's documented override for a user-level server), loaded with `--approve` for
  that process. The operator's config is never edited.
- `--tools` lists exactly the four fetch tools: no read, write, bash or anything else.
- Pi cuts tool results over 20 KB before the model sees them, and the stream carries the cut text. The full
  result is saved to a file named in the result's `details.fullOutputPath`, and that file is what is used.
- Each turn is one short `axon pi -p` process (no session file). Pi waits for servers with direct tools before
  the first model call, so there is no warm-up turn and no `pending` polling as on Claude.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .. import observe
from .atlassian import TOOLS, TURN_TIMEOUT_S, AtlassianError, ToolCall, TurnResult, _result_text, pick_cloud_id

DEFAULT_SERVER = "atlassian"
# Pi's hermetic set minus --no-approve: the one project file this session writes must be trusted
FLAGS = ("--approve", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files", "--no-session")


class AxonConnectorSession:
    """Atlassian through `axon pi`. Use as a context manager, exactly like `ConnectorSession`."""

    def __init__(self, log_path: Path, *, axon_bin: str = "axon", model: str | None = None,
                 provider: str | None = None, server: str = DEFAULT_SERVER, workdir: Path | None = None):
        self.log_path = log_path
        self.axon_bin = axon_bin
        self.model = model
        self.provider = provider
        self.server = server
        self.prefix = f"mcp__{server}__"
        self.workdir = workdir or log_path.parent / "atlassian-axon"
        self.connector_status = "unknown"
        self.cloud_id: str | None = None

    # ---- process plumbing --------------------------------------------------------------------
    def __enter__(self) -> AxonConnectorSession:
        (self.workdir / ".pi").mkdir(parents=True, exist_ok=True)
        (self.workdir / ".pi" / "mcp.json").write_text(
            json.dumps({"mcpServers": {self.server: {"exposure": "direct"}}}, indent=2), encoding="utf-8")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc) -> None:
        return None  # every turn is its own process, already finished

    def argv(self) -> list[str]:
        from harness.engines.pi import _assert_known_flags

        argv = [self.axon_bin, "pi", "--mode", "json", "-p", *FLAGS]
        if self.provider:
            argv += ["--provider", self.provider]
        if self.model:
            argv += ["--model", self.model]
        argv += ["--tools", ",".join(self.prefix + t for t in TOOLS)]
        _assert_known_flags(argv[1:])
        return argv

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Axon sets these for its own Pi; inherited from an enclosing Axon session they would point it astray
        for k in ("PI_OFFLINE", "PI_SKIP_VERSION_CHECK", "PI_CODING_AGENT_DIR"):
            env.pop(k, None)
        return env

    def _turn(self, text: str) -> TurnResult:
        """One prompt, recorded on the trace as `agent:atlassian` like the Claude session's turns."""
        res, err = TurnResult(), ""
        try:
            res = self._turn_raw(text)
            return res
        except AtlassianError as e:
            err = str(e)
            raise
        finally:
            observe.llm_call(
                "atlassian", model=self.model or "", prompt=text, output=f"{len(res.calls)} connector call(s)",
                cost_usd=res.cost_usd, seconds=res.seconds, error=err,
                tool_calls=[{"name": c.name, "input": c.input, "output": c.text, "is_error": c.is_error}
                            for c in res.calls])

    def _turn_raw(self, text: str) -> TurnResult:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(self.argv(), input=text, capture_output=True, text=True, cwd=self.workdir,
                                  env=self.env(), timeout=TURN_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            raise AtlassianError(f"the Axon connector call did not finish within {TURN_TIMEOUT_S}s",
                                 retryable=True) from e
        except FileNotFoundError as e:
            raise AtlassianError(f"`{self.axon_bin}` is not installed or not on PATH") from e
        with open(self.log_path, "a", encoding="utf-8") as log:
            log.write(proc.stdout)
        if proc.stderr.strip():
            with open(self.log_path.with_suffix(".stderr"), "a", encoding="utf-8") as log:
                log.write(proc.stderr)
        res = TurnResult(seconds=round(time.monotonic() - t0, 1))
        by_id: dict[str, ToolCall] = {}
        settled = False
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "agent_settled":
                settled = True
            if ev.get("type") != "message_end":
                continue
            msg = ev.get("message") or {}
            if msg.get("role") == "assistant":
                cost = ((msg.get("usage") or {}).get("cost") or {}).get("total")
                res.cost_usd += float(cost or 0)
                for b in msg.get("content") or []:
                    name = str(b.get("name", ""))
                    if b.get("type") == "toolCall" and name.startswith(self.prefix):
                        by_id[b.get("id")] = ToolCall(name[len(self.prefix):], b.get("arguments") or {})
                        res.calls.append(by_id[b.get("id")])
                if msg.get("stopReason") in ("error", "aborted"):
                    raise AtlassianError(f"axon refused the request: {msg.get('errorMessage') or msg.get('stopReason')}"
                                         " — not a connector problem; rerun once that is resolved")
            elif msg.get("role") == "toolResult" and msg.get("toolCallId") in by_id:
                call = by_id[msg["toolCallId"]]
                call.text = self._full_text(msg)
                call.is_error = bool(msg.get("isError"))
        if not settled:
            tail = (proc.stderr.strip().splitlines() or ["no output"])[-1]
            raise AtlassianError(f"axon pi exited {proc.returncode} without finishing the turn: {tail}",
                                 retryable=True)
        return res

    @staticmethod
    def _full_text(msg: dict) -> str:
        """The untruncated result: Pi saves results over 20 KB to `details.fullOutputPath`."""
        path = (msg.get("details") or {}).get("fullOutputPath") if isinstance(msg.get("details"), dict) else None
        if path:
            try:
                return Path(path).read_text(encoding="utf-8")
            except OSError:
                pass  # fall back to what the stream carried; parse_page then fails loudly on cut JSON
        return _result_text(msg.get("content"))

    def server_status(self) -> str:
        """The server's state per `axon pi mcp list --json` (connected / needs-auth / failed / missing)."""
        try:
            out = subprocess.run([self.axon_bin, "pi", "mcp", "list", "--json"], capture_output=True, text=True,
                                 env=self.env(), timeout=120).stdout
            data = json.loads(out)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return "unknown"
        rows = data.get("servers") if isinstance(data, dict) else data
        for row in rows or []:
            if isinstance(row, dict) and row.get("name") == self.server:
                return str(row.get("state") or row.get("status") or "unknown")
        return "missing"

    # ---- public API (same as ConnectorSession) -------------------------------------------------
    def connect(self, site: str) -> TurnResult:
        """Resolve the cloudId. Raises when the server is missing or not signed in."""
        res = self._turn("Call getAccessibleAtlassianResources once. Do not call anything else. Reply: done.")
        for c in res.calls:
            if c.name == "getAccessibleAtlassianResources" and not c.is_error:
                self.cloud_id = pick_cloud_id(c.text, site)
        if self.cloud_id:
            self.connector_status = "connected"
            return res
        self.connector_status = self.server_status()
        if self.connector_status == "missing":
            raise AtlassianError(
                f"Axon has no MCP server named '{self.server}'. Add it once: "
                f"`axon pi mcp add {self.server} --url https://mcp.atlassian.com/v1/mcp`, then "
                f"`axon pi mcp login {self.server}`, and rerun scope.")
        if "auth" in self.connector_status or "sign" in self.connector_status:
            raise AtlassianError(
                f"Axon's '{self.server}' MCP server is not signed in (status '{self.connector_status}'). Run "
                f"`axon pi mcp login {self.server}` once, then rerun scope.")
        raise AtlassianError(
            f"Axon's Atlassian server gave no getAccessibleAtlassianResources result (status "
            f"'{self.connector_status}'). Atlassian's server is often just slow to answer: rerun in a minute.",
            retryable=True)

    def fetch(self, pages: list[str] = (), page_descendants: list[str] = (), jira: list[str] = ()) -> TurnResult:
        steps = [f"getConfluencePage with cloudId {self.cloud_id}, pageId {p}, contentFormat markdown" for p in pages]
        steps += [f"getConfluencePageDescendants with cloudId {self.cloud_id}, pageId {p}" for p in page_descendants]
        steps += [f"getJiraIssue with cloudId {self.cloud_id}, issueIdOrKey {k}" for k in jira]
        if not steps:
            return TurnResult()
        prompt = ("Make exactly these tool calls (in parallel where you can), nothing else, and do not "
                  "summarise their output:\n- " + "\n- ".join(steps) + "\nThen reply: done.")
        return self._turn(prompt)
