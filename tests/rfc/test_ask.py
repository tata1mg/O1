
FINDINGS = """# findings
## Data model
stuff
## Open questions
1. Who owns the pre-warm script and where is the top-100 list sourced from?
- Should the cache TTL stay at 3 days as shipped in code today?
## Not a question section
- ignore me
"""








def test_log_operator_action_shape(tmp_path):
    """CLI-native asks must write the operator-actions file themselves (previously prompt-enforced
    via the slash-command markdown only) — obs flushes it as operator:* events next invocation."""
    import json

    from rfc_agent.ask import log_operator_action

    log_operator_action(tmp_path, "answers_given", answered=2, asked=5)
    rec = json.loads((tmp_path / "logs" / "operator_actions.jsonl").read_text().strip())
    assert rec["action"] == "answers_given"
    assert rec["detail"] == {"answered": 2, "asked": 5}
    assert rec["ts"]
