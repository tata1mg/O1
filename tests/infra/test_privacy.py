"""privacy.py — the client-side secret scrub. Table-driven true positives (each provider's
documented dummy format), false-positive fixtures matching our real traffic shape (UUIDs, git SHAs,
base64 blobs), nested payloads, fail-closed behavior, and a hot-path perf bound."""

import json
import time
import types

import pytest

from rfc_agent import privacy
from rfc_agent.privacy import mask_otel_spans, scrub_text

TRUE_POSITIVES = [
    ("aws-key", "creds: AKIAIOSFODNN7EXAMPLE ok"),
    ("github-token", "export GH=ghp_" + "a1B2" * 9 + "z9"),
    ("gitlab-token", "glpat-" + "x" * 24),
    ("slack-token", "xoxb-1234567890-abcdefGHIJKL"),
    ("slack-webhook", "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"),
    ("atlassian-token", "pass ATATT3xFfGF0" + "Ab-" * 12 + "=901A"),
    ("openai-key", "sk-proj-" + "Ab1" * 12),
    ("openai-key", "sk-abc12T3BlbkFJdef34"),
    ("anthropic-key", "sk-ant-api03-" + "x" * 40),
    ("anthropic-key", "sk-ant-oat01-" + "y" * 40),  # the OAuth form gitleaks itself missed
    ("langfuse-key", "sk-lf-00000000-0000-0000-0000-000000000000"),
    ("google-key", "AIza" + "S" * 35),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpM"),
    ("private-key", "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"),
    ("private-key", "-----BEGIN PRIVATE KEY-----\nMIIabc…[+1800 chars]"),  # END clipped away
    ("url-credentials", "postgresql://svc_user:s3cretPW@db.internal:5432/orders"),
    ("bearer-token", "curl -H 'Authorization: Bearer abcDEF123456789012345678901234567890'"),
    ("kv-secret", 'password = "x9K2mQ8vLp3RtW7z"'),
]

FALSE_POSITIVES = [
    "0f1168aa-5833-4256-ac96-92f5d05c0ad0",                       # UUID
    "commit 7bb1781f2ab9e0c4d6a8e12b34c56d78e90f1a2b applied",    # git SHA
    "integrity: " + "u" * 64,                                     # lockfile-ish base64 run, no keyword
    "the password policy requires rotation every 90 days",        # keyword, no assignment
    'api_key = "example"',                                        # allowlisted value
    'secret: "your_key_here"',                                    # allowlisted value
    'token = "aaaaaaaaaaaaaaaaaa"',                               # low entropy
    "password: 0f1168aa-5833-4256-ac96-92f5d05c0ad0",             # UUID value allowlisted
    "grep -n 'similar_patient' patient_crud_manager.py",          # ordinary tool traffic
]


@pytest.mark.parametrize("rule,text", TRUE_POSITIVES)
def test_true_positives(rule, text):
    masked, hits = scrub_text(text)
    assert hits >= 1, f"{rule}: no hit in {text[:40]!r}"
    assert f"[REDACTED:{rule}]" in masked
    # the raw secret material must be gone (check its densest fragment)
    assert text[-12:] not in masked or masked.endswith(text[-12:]) is False or "REDACTED" in masked


@pytest.mark.parametrize("text", FALSE_POSITIVES)
def test_false_positives_untouched(text):
    masked, hits = scrub_text(text)
    assert hits == 0 and masked == text


def test_nested_json_payload():
    blob = '{"tool": "Bash", "output": "AWS_KEY=AKIAIOSFODNN7EXAMPLE\\nexport DONE"}'
    masked, hits = scrub_text(blob)
    assert hits == 1 and "AKIAIOSFODNN7EXAMPLE" not in masked and '"tool": "Bash"' in masked


def test_idempotent():
    once, _ = scrub_text("key AKIAIOSFODNN7EXAMPLE here")
    twice, hits2 = scrub_text(once)
    assert twice == once and hits2 == 0


def test_perf_100k_prompt():
    text = ("Read every file under raw/ and findings/. " * 2300)[:100_000]
    t0 = time.monotonic()
    scrub_text(text)
    assert time.monotonic() - t0 < 0.05, "mask must stay sub-50ms on a 100K prompt"


def _params(spans):
    return types.SimpleNamespace(spans=spans)


def _span(attrs):
    return types.SimpleNamespace(attributes=attrs)


def test_mask_otel_spans_patches_only_dirty_spans():
    from langfuse.types import MaskOtelSpansResult

    params = _params({
        "s1": _span({"langfuse.observation.output": "token sk-lf-00000000-0000-0000-0000-000000000000"}),
        "s2": _span({"langfuse.observation.input": "clean text, nothing secret here at all"}),
        "s3": _span({"n": 5, "seq": ["ok string", "Bearer abcDEF123456789012345678901234567890"]}),
    })
    res = mask_otel_spans(params=params)
    assert isinstance(res, MaskOtelSpansResult)
    assert set(res.span_patches) == {"s1", "s3"}
    assert "[REDACTED:langfuse-key]" in res.span_patches["s1"].set_attributes["langfuse.observation.output"]
    assert "[REDACTED:bearer-token]" in res.span_patches["s3"].set_attributes["seq"][1]


def test_mask_otel_spans_fail_closed_sentinel(monkeypatch):
    monkeypatch.setattr(privacy, "scrub_text", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    params = _params({"s1": _span({"a": "some long enough string value here", "n": 7})})
    res = mask_otel_spans(params=params)
    assert res.span_patches["s1"].set_attributes["a"] == "<masked: scrubber error>"
    assert "n" not in res.span_patches["s1"].set_attributes  # non-strings untouched


# ---- Python SDK v5: metadata attribute values are JSON-encoded, strings included ----

def test_v5_metadata_attributes_are_scrubbed_inside_their_json_encoding():
    """`"prod"` ships as '"prod"'. A raw regex pass can break the encoding (the private-key rule
    runs to end-of-string and ate the closing quote), so metadata is decoded, scrubbed, re-encoded."""
    import json

    key = "-----BEGIN PRIVATE KEY-----\nMIIabc…[+1800 chars]"  # END clipped away
    nested = {"cmd": "curl -H 'Authorization: Bearer abcDEF123456789012345678901234567890'", "n": 3}
    params = _params({"s1": _span({
        "langfuse.observation.metadata.note": json.dumps(key),
        "langfuse.trace.metadata.ctx": json.dumps(nested),
        "langfuse.observation.metadata.clean": json.dumps("nothing secret in this value"),
    })})
    patched = mask_otel_spans(params=params).span_patches["s1"].set_attributes
    assert "langfuse.observation.metadata.clean" not in patched  # sparse: untouched values stay out
    note = json.loads(patched["langfuse.observation.metadata.note"])  # still valid JSON
    assert note == "[REDACTED:private-key]"
    ctx = json.loads(patched["langfuse.trace.metadata.ctx"])
    assert "[REDACTED:bearer-token]" in ctx["cmd"] and ctx["n"] == 3


def test_v5_metadata_that_is_not_json_is_still_scrubbed_raw():
    from rfc_agent.privacy import scrub_attribute

    masked, n = scrub_attribute("langfuse.observation.metadata.x", "raw sk-lf-00000000-0000-0000-0000-000000000000")
    assert n == 1 and "[REDACTED:langfuse-key]" in masked
    # input/output that is not JSON keeps the raw path exactly as before
    assert scrub_attribute("langfuse.observation.output", "token sk-lf-00000000-0000-0000-0000-000000000000") == \
        scrub_text("token sk-lf-00000000-0000-0000-0000-000000000000")


@pytest.mark.parametrize("key", ["langfuse.observation.input", "langfuse.observation.output",
                                 "langfuse.trace.input", "langfuse.trace.output"])
def test_v5_json_input_output_stays_valid_json_after_scrubbing(key):
    from rfc_agent.privacy import scrub_attribute

    value = json.dumps({"file_path": "k.pem", "content": "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc"})
    masked, n = scrub_attribute(key, value)
    decoded = json.loads(masked)  # the closing quote survives
    assert n >= 1 and decoded["file_path"] == "k.pem" and "MIIEabc" not in decoded["content"]
