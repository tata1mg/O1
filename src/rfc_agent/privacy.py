"""Client-side secret redaction: nothing credential-shaped leaves this machine unmasked.

The exposure (2026-08-17 analysis): tool spans carry Bash/Read io — an agent that cats a real
config.json would transmit live credentials to Langfuse Cloud. The 719-observation sweep of every
existing trace found zero leaks, but that was luck (2K clips + agents reading templates), not a
control. This module is the control.

Design (industry research, 2026-08-17 — Langfuse masking docs + gitleaks ruleset + LangSmith/
Braintrust conventions):
- Hook = `mask_otel_spans`, the EXPORT-stage hook: it sees the raw attributes of every span this
  client exports — SDK spans, `propagate_attributes` metadata, and any future third-party OTel
  instrumentation. (The legacy `mask` hook has a documented third-party-span hole, langfuse#11222.)
- Ruleset = deterministic, compiled, gitleaks-derived HIGH-SIGNAL patterns only. Free-standing
  entropy scanning is deliberately absent (documented false-positive storms on hashes/UUIDs/base64 —
  exactly our tool-output traffic); entropy is used solely as a secondary gate on the one generic
  keyword=value rule, the only FP-prone class.
- Substring pre-checks gate every regex (gitleaks practice) so the common case is a few `in` scans.
- FAIL-CLOSED at two levels: a span whose scrub errors gets its string attributes replaced with a
  sentinel; if the hook itself breaks, the raised exception makes the SDK drop the export batch
  (documented behavior) — raw bytes never ship.
"""

from __future__ import annotations

import math
import re

REDACTED = "[REDACTED:{rule}]"
_SENTINEL = "<masked: scrubber error>"
_MIN_SCAN_LEN = 16  # shortest matchable secret in the ruleset (AKIA + 16)

# (rule, substring pre-checks (any-of, lowercase haystack), compiled pattern)
_RULES: list[tuple[str, tuple[str, ...], re.Pattern]] = [
    ("aws-key", ("akia", "asia", "abia", "acca"),
     re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b")),
    ("github-token", ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b")),
    ("gitlab-token", ("glpat-",), re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("slack-token", ("xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-"),
     re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("slack-webhook", ("hooks.slack.com",),
     re.compile(r"https://hooks\.slack\.com/services/T[A-Za-z0-9_/]{16,}")),
    ("atlassian-token", ("atatt",), re.compile(r"\bATATT[A-Za-z0-9_\-=]{20,}")),
    ("openai-key", ("t3blbkfj", "sk-proj-", "sk-svcacct-", "sk-admin-"),
     re.compile(r"\bsk-[A-Za-z0-9_\-]*T3BlbkFJ[A-Za-z0-9_\-]*\b"
                r"|\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}\b")),
    # api03/admin01 + the oat01/ort01 OAuth tokens gitleaks itself missed (gitleaks#2158)
    ("anthropic-key", ("sk-ant-",),
     re.compile(r"\bsk-ant-(?:api03|admin01|oat01|ort01)-[A-Za-z0-9_\-]{20,}")),
    ("langfuse-key", ("sk-lf-",), re.compile(r"\bsk-lf-[a-f0-9\-]{30,}\b")),
    ("google-key", ("aiza",), re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", ("eyj",),
     re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    # the whole block goes, BEGIN through END (or end-of-string when the END is clipped away)
    ("private-key", ("-----begin",),
     re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY[A-Z ]*-----"
                r"(?:.*?-----END[A-Z ]*PRIVATE KEY[A-Z ]*-----|.*$)", re.DOTALL)),
    ("url-credentials", ("://",),
     re.compile(r"\b[a-z][a-z0-9+.\-]*://[^:/\s@'\"]+:[^@/\s'\"]{4,}@")),
    ("bearer-token", ("earer ",),  # matches Bearer/bearer without a case-insensitive haystack pass
     re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{25,}")),
]

# the one FP-prone rule: keyword + assignment + long value, gated by entropy and an allowlist
_KV_PRECHECK = ("password", "passwd", "secret", "api_key", "apikey", "api-key",
                "access_token", "auth_token", "credential")
_KV_RE = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|credential)s?\b"
    r"\s*[=:]\s*['\"]?([A-Za-z0-9+/._\-]{12,})")
_KV_ALLOW = re.compile(
    r"(?i)^(?:example|placeholder|changeme|change_me|dummy|sample|test|redacted|template|"
    r"your[_-].*|xxx+.*|\$\{.*|<.*>|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_HEX_RE = re.compile(r"^[0-9a-f]{32,}$", re.IGNORECASE)  # git SHAs / digests are not secrets


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def scrub_text(text: str) -> tuple[str, int]:
    """Redact secret-shaped substrings. Returns (masked_text, hit_count)."""
    if len(text) < _MIN_SCAN_LEN:
        return text, 0
    hits = 0
    low = text.lower()
    for rule, prechecks, pattern in _RULES:
        if not any(p in low for p in prechecks):
            continue
        text, n = pattern.subn(REDACTED.format(rule=rule), text)
        if n:
            hits += n
            low = text.lower()
    if any(p in low for p in _KV_PRECHECK):
        def _kv_sub(m: re.Match) -> str:
            nonlocal hits
            value = m.group(2)
            if _KV_ALLOW.match(value) or _HEX_RE.match(value) or _entropy(value) < 3.5:
                return m.group(0)
            hits += 1
            return m.group(0)[: m.start(2) - m.start(0)] + REDACTED.format(rule="kv-secret")
        text = _KV_RE.sub(_kv_sub, text)
    return text, hits


def mask_otel_spans(*, params):
    """Export-stage hook for `Langfuse(mask_otel_spans=...)` — scrubs every string attribute of
    every span in the batch. Sparse patches; per-span fail-closed sentinel; a hook-level error
    propagates, which makes the SDK drop the batch (documented) — never fail-open."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for ident, span in params.spans.items():
        try:
            changed = {}
            for key, value in (span.attributes or {}).items():
                if isinstance(value, str) and len(value) >= _MIN_SCAN_LEN:
                    masked, n = scrub_text(value)
                    if n:
                        changed[key] = masked
                elif isinstance(value, (list, tuple)):
                    out, dirty = [], False
                    for item in value:
                        if isinstance(item, str) and len(item) >= _MIN_SCAN_LEN:
                            masked, n = scrub_text(item)
                            out.append(masked)
                            dirty = dirty or bool(n)
                        else:
                            out.append(item)
                    if dirty:
                        changed[key] = out
            if changed:
                patches[ident] = OtelSpanPatch(set_attributes=changed)
        except Exception:  # noqa: BLE001 — fail-closed: this span's strings never ship raw
            patches[ident] = OtelSpanPatch(set_attributes={
                key: _SENTINEL for key, value in (span.attributes or {}).items()
                if isinstance(value, str)
            })
    return MaskOtelSpansResult(span_patches=patches)
