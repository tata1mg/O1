"""Investigate settings: preset defaults, then config.json INVESTIGATE.*, then CLI overrides (docs/INVESTIGATE.md)."""

from __future__ import annotations

from ..legacy_names import config_section

PRESETS: dict[str, dict] = {
    "balanced": {
        "TRACER_SPLIT": "flow",
        "MAX_TRACERS": 6,
        "SCREENS_PER_READER": 50,
        "ENTRY_PREPASS": False,
        "TRACER_EFFORT": "medium",
        "TRACER_CALL_BUDGET": 80,
    },
    "fast": {
        "TRACER_SPLIT": "hop",
        "MAX_TRACERS": 12,
        "SCREENS_PER_READER": 15,
        "ENTRY_PREPASS": True,
        "TRACER_EFFORT": "low",
        "TRACER_CALL_BUDGET": 40,
    },
}

BASE: dict = {
    "PRESET": "balanced",
    "REQ_GRANULARITY": "grouped",
    "ASK": False,
    "LEAD_MODEL": "claude-opus-5-5[1m]",
    "LEAD_EFFORT": "high",
    "TRACER_MODEL": "claude-sonnet-5-5",
    "UI_MODEL": "claude-opus-5-5",
    "UI_EFFORT": "medium",
    "LEAD_CODE_READS": 15,
    "TRACER_TIMEOUT_S": 900,
    "UI_TIMEOUT_S": 600,
    "LEAD_TIMEOUT_S": 1200,
}

GRANULARITY = ("grouped", "fine")


def resolve(cfg, overrides: dict | None = None) -> dict:
    """Effective settings. Precedence: CLI overrides > config.json INVESTIGATE > preset > base."""
    user = config_section(cfg, "INVESTIGATE")
    user = {k: v for k, v in user.items() if not str(k).startswith("_")}
    ov = {k: v for k, v in (overrides or {}).items() if v is not None}
    preset = str(ov.get("PRESET") or user.get("PRESET") or BASE["PRESET"])
    if preset not in PRESETS:
        raise SystemExit(f"investigate: unknown preset {preset!r} — use one of {', '.join(PRESETS)}")
    out = {**BASE, **PRESETS[preset], **user, **ov, "PRESET": preset}
    if out["REQ_GRANULARITY"] not in GRANULARITY:
        raise SystemExit(f"investigate: REQ_GRANULARITY must be one of {', '.join(GRANULARITY)}")
    return out
