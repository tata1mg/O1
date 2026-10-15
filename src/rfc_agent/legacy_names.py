"""The stages' old names, so config and run folders written before the rename keep working.

Until 0.38.0 the stages were called prep and stage1..stage5. A config.json may still carry STAGE1..STAGE5
sections, and a run folder from then holds files like work/stage1.state.json or logs/prep.json. Both are
read under the new names without the operator doing anything.
"""

from __future__ import annotations

from pathlib import Path

OLD_TO_NEW = {
    "prep": "scope",
    "stage1": "investigate",
    "stage2": "draft",
    "stage3": "review",
    "stage4": "revise",
    "stage5": "summarize",
}
OLD_CONFIG = {"INVESTIGATE": "STAGE1", "DRAFT": "STAGE2", "REVIEW": "STAGE3", "REVISE": "STAGE4", "SUMMARIZE": "STAGE5"}
OLD_FROM = {"0": "scope", "1": "investigate", "2": "draft", "3": "review", "4": "revise"}
_RUN_SUBDIRS = ("work", "logs", "confirm")


def config_section(cfg, name: str) -> dict:
    """config.json section `name`, falling back to its old STAGE<n> spelling."""
    if not cfg:
        return {}
    return cfg.get(name) or cfg.get(OLD_CONFIG.get(name, "")) or {}


def migrate_run_dir(run_dir: Path) -> list[str]:
    """Rename old-named files in a run folder (work/stage1.state.json -> work/investigate.state.json, ...).

    Only files whose name starts with an old stage name followed by "." or "-" are touched, and never over
    an existing new-named file. Returns the renames done, as "old -> new" lines.
    """
    done: list[str] = []
    for sub in _RUN_SUBDIRS:
        d = Path(run_dir) / sub
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            for old, new in OLD_TO_NEW.items():
                if f.name.startswith((old + ".", old + "-")):
                    target = f.with_name(new + f.name[len(old):])
                    if not target.exists():
                        f.rename(target)
                        done.append(f"{sub}/{f.name} -> {sub}/{target.name}")
                    break
    return done
