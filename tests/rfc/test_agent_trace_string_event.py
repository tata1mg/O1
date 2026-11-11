"""A system stream event whose `message` is a plain string (e.g. permission_denied) must not crash the verbose
tracer. Regression: the review stage died on `'str' object has no attribute 'get'` at observe.AgentTrace.event."""

from rfc_agent import observe


def test_string_message_event_is_ignored_by_verbose_tracer(monkeypatch):
    monkeypatch.setattr(observe, "_verbose", True)
    trace = observe.AgentTrace(agent=object(), gen=None)
    trace.event({"type": "system", "subtype": "permission_denied", "tool_name": "mcp__fleetlens__get_source",
                 "message": "Claude requested permissions to use mcp__fleetlens__get_source, "
                            "but you haven't granted it yet."})
    assert trace.tools == {}


def test_object_message_event_still_reads_content(monkeypatch):
    monkeypatch.setattr(observe, "_verbose", True)
    monkeypatch.setattr(observe, "_child", lambda *a, **k: None)
    trace = observe.AgentTrace(agent=object(), gen=None)
    trace.event({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
    assert trace.tools == {}
