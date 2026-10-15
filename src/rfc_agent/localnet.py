"""Request boundary for local-process-only loopback HTTP servers.

A 127.0.0.1 bind is NOT a request boundary (established 0.24.1, tools/feed_server.py): any web
page the operator has open can land a no-preflight "simple" POST on a loopback port, and DNS
rebinding delivers requests the browser believes are same-origin. Two checks close both holes
for a server whose only legitimate client is a LOCAL NON-BROWSER PROCESS:

- reject ANY Origin header: browsers attach Origin to every cross-origin request and to every
  same-origin non-GET; a local process (the claude child via ANTHROPIC_BASE_URL, curl, the CLI)
  never sends one. NOTE this is the opposite policy from a server that SERVES a browser page —
  the feed server's own page must be allowlisted by origin instead (steer_request_ok in
  tools/feed_server.py, kept import-free there so the script stays bare-python runnable).
- require a loopback Host: kills DNS rebinding, where Origin can be absent (a rebound GET) but
  Host carries the attacker's domain.
"""

from __future__ import annotations


def local_request_ok(headers) -> tuple[bool, str]:
    """(ok, reason). `headers` is any case-insensitive mapping (http.server's Message headers)."""
    if headers.get("Origin") is not None:
        return False, "browser-origin request (Origin header present)"
    host = (headers.get("Host") or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        return False, f"non-loopback host ({host!r})"
    return True, ""
