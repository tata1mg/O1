"""Investigate's per-repo index facts from the context layer (fleetlens): code index yes/no, index age."""

import json

from rfc_agent.investigate import repo_info


def _fake_layer(monkeypatch, indexed: set[str], indexed_at: str | None):
    calls = []

    def tool_result(url, headers, tool, args):
        calls.append(tool)
        if tool == "find_symbol":
            repo = args["query"].split(":")[1]
            return {"content": [{"type": "text", "text": json.dumps({"count": 1 if repo in indexed else 0})}]}
        if tool == "get_index_info" and indexed_at:
            return {"content": [{"type": "text", "text": json.dumps({"indexed_at": indexed_at})}]}
        return None

    monkeypatch.setattr(repo_info, "_tool_result", tool_result)
    return calls


SCOPE = {"repos": [{"repo": "svc_a", "commit_date": "2026-10-12T14:00:00+00:00"},
                   {"repo": "merch_service", "commit_date": "2026-10-10T09:00:00+00:00"},
                   {"repo": "web"}]}


def test_code_index_and_age_against_each_snapshot(monkeypatch):
    calls = _fake_layer(monkeypatch, {"svc_a", "merch_service"}, "2026-10-12 12:11:40")
    out = repo_info.index_info({"url": "u"}, {}, SCOPE)
    assert out["svc_a"]["code_index"] and not out["web"]["code_index"]
    assert "before the snapshot's last commit (2026-10-12 14:00) — the code wins" in out["svc_a"]["age_note"]
    assert out["merch_service"]["age_note"] == "index built 2026-10-12 12:11, after the snapshot's last commit"
    assert out["web"]["age_note"] == "index built 2026-10-12 12:11"
    assert calls.count("get_index_info") == 1, "one build date covers the whole fleet"


def test_no_layer_or_no_build_date_means_unknown(monkeypatch):
    assert repo_info.index_info(None, {}, SCOPE)["svc_a"] == {"code_index": False, "age_note": "unknown"}
    _fake_layer(monkeypatch, {"svc_a"}, None)
    out = repo_info.index_info({"url": "u"}, {}, SCOPE)
    assert out["svc_a"]["code_index"] and out["svc_a"]["age_note"] == "unknown"
