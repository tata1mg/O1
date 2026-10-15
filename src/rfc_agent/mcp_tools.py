"""Per-server READ-ONLY MCP tool allowlists.

Why: `--allowedTools mcp__bitbucket` grants EVERY tool of that server — including create_pull_request,
merge_pull_request and create_branch — to an agent whose context is full of untrusted ingested text
(a Confluence PRD, a grooming transcript). Same for atlassian's editJiraIssue/createConfluencePage.
That is the "lethal trifecta" (private data + untrusted content + action capability) in one stage, and the
consensus mitigation is architectural: remove the action capability. These allowlists are that removal —
stages get named read tools only, so an injected instruction has nothing to call.

Adding a server: add its read-only tools here. Unknown servers are DENIED (EngineFailure), deliberately —
a missing entry must fail loudly rather than silently fall back to a whole-server grant.
"""

from __future__ import annotations

from .contracts import EngineFailure

# Tools that are NOT read-only but are granted anyway because a PreToolUse hook constrains every
# invocation to a fixed read-only form. Each entry names its enforcement so the security test can
# hold the exception accountable instead of the read-only invariant silently eroding.
HOOK_GUARDED_TOOLS: dict[str, str] = {
    # arbitrary-JS Plugin API tool; guard_writes.py permits ONLY the fixed enumeration scripts in
    # USE_FIGMA_SCRIPTS (page list / sibling inventory / children inventory), node id substituted
    "use_figma": "rfc_agent.hooks.guard_writes.USE_FIGMA_SCRIPTS",
}

READONLY_TOOLS: dict[str, tuple[str, ...]] = {
    "bitbucket": (
        "search_code",
        "get_file_content",
        "get_repository",
        "list_repositories",
        "list_branches",
        "list_commits",
        "get_commit",
    ),
    # the context layer (replaced context-service 2026-10-12). Every tool fleetlens 1.30 ships is a read.
    "fleetlens": (
        "get_service_graph",
        "list_services",
        "get_index_info",
        "discover_services",
        "discover_interfaces",
        "list_interfaces",
        "get_service_relationships",
        "get_endpoint_call_graph",
        "get_callers",
        "get_callees",
        "get_symbol",
        "find_symbol",
    ),
    "gsheets": (
        # instrumentation-sheet inventory needs READS only — the server also ships ~35 write tools
        # (append/update/clear/format/delete) that must never reach an agent reading untrusted PRDs
        "sheets_check_access",
        "sheets_get_metadata",
        "sheets_get_values",
        "sheets_batch_get_values",
        "sheets_get_sheet_structure",
        "sheets_get_sheet_dimensions",
    ),
    "atlassian": (
        "getConfluencePage",
        "getConfluencePageDescendants",
        "getConfluencePageFooterComments",
        "getConfluencePageInlineComments",
        "getJiraIssue",
        "searchConfluenceUsingCql",
        "searchJiraIssuesUsingJql",
        "getAccessibleAtlassianResources",
        "search",
        "fetch",
    ),
    "figma": (
        "get_design_context",
        "get_screenshot",
        "get_metadata",
        "get_variable_defs",
        "get_figjam",
        # use_figma is WRITE-capable (arbitrary Plugin API JS) — granted anyway because it is the
        # only reliable large-file enumeration channel (get_metadata's page list silently truncated
        # 25 pages to 1, 2026-08-25). Containment is the guard_writes hook: only the fixed read-only
        # enumeration scripts in USE_FIGMA_SCRIPTS pass; everything else is denied at PreToolUse.
        # Served by mcp.figma.com under the operator's OAuth (surfaces as mcp__claude_ai_Figma__* in
        # stages — the local 3845 bridge does not carry it), probe-verified headless 2026-08-25.
        "use_figma",
    ),
}


# The same service reachable under user-scope names (Stage.inherit_user_mcp): the claude.ai Atlassian
# connector and the official figma plugin expose the SAME tool names under these prefixes. Grants are
# still per-tool — an alias never widens WHICH tools are callable, only which server name serves them.
USER_SCOPE_ALIASES: dict[str, tuple[str, ...]] = {
    "atlassian": ("claude_ai_Atlassian",),
    # Figma: grant every prefix it can arrive under, because which one is live depends on operator setup.
    # The figma PLUGIN (`plugin_figma_figma`) doesn't work headless (probed 2026-07-27) — plugin MCPs
    # aren't loaded for `claude -p` stages. The desktop Dev Mode server (127.0.0.1:3845/mcp) DOES work
    # headless as of 2026-08-11: the 2026-07-27 "never finishes connecting" was the operator's VIEW seat
    # (the server is seat-gated) — with a Dev seat a `claude -p` stage connects and lists all six tools.
    # It is now the default `figma` server (config keeps `figma-remote` = mcp.figma.com as fallback).
    # NOTE: rate limits are IDENTICAL local vs remote (both assume the seat: 15-20/min, 200-600/day,
    # attributed to the team OWNING the file) — local buys no-OAuth + org-block immunity, not quota.
    # Requires the desktop app RUNNING; doctor probes it. The aliases stay as harmless fallbacks.
    "figma": ("plugin_figma_figma", "claude_ai_Figma"),
}


def allowed_tools(servers: tuple[str, ...]) -> list[str]:
    """Explicit `mcp__<server>__<tool>` grants for the stage's servers. Never a whole-server grant."""
    out: list[str] = []
    for s in servers:
        tools = READONLY_TOOLS.get(s)
        if tools is None:
            raise EngineFailure(
                f"MCP server '{s}' has no read-only tool allowlist in mcp_tools.READONLY_TOOLS — refusing to "
                f"grant it (a whole-server grant would hand write tools to an agent reading untrusted input). "
                f"Add its read-only tools there."
            )
        for prefix in (s, *USER_SCOPE_ALIASES.get(s, ())):
            out += [f"mcp__{prefix}__{t}" for t in tools]
    return out
