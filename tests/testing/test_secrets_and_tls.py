"""Credential handling and TLS scope.

Covers review blocker 1.1 (the Kiwi password reaching `flags.json` and any captured CI log) and
should-fix 5.5 (`insecure_tls` disabling verification process-wide, permanently).
"""

from __future__ import annotations

import dataclasses
import ssl

import pytest

from harness.flags import Flag, registry, resolve
from workflows.testing import flags as testing_flags
from workflows.testing.store.kiwi import KiwiCaseStore

SECRET_SUPPORTED = testing_flags._SECRET_SUPPORTED
needs_secret_field = pytest.mark.skipif(
    not SECRET_SUPPORTED,
    reason="harness.flags.Flag has no `secret` field yet (kernel blocker 1.1 not merged)")


# ---- the workflow's declaration, which stands either way ---------------------------------------------
def test_the_password_is_declared_a_secret():
    assert "testing.kiwi_password" in testing_flags.SECRETS


def test_the_username_is_deliberately_not_a_secret():
    """It is written to the Kiwi TestRun's `manager` field, where every account can already read it, and
    "which account did this run use" is the first question when writes land in the wrong place."""
    assert "testing.kiwi_user" not in testing_flags.SECRETS


def test_every_declared_secret_is_a_flag_this_workflow_registers():
    assert set(testing_flags.SECRETS) <= set(testing_flags.NAMES)


def test_no_flag_holding_a_credential_is_left_undeclared():
    """A new `testing.*` flag whose name says credential must be added to SECRETS, not just registered."""
    suspicious = [n for n in testing_flags.NAMES
                  if any(w in n for w in ("password", "secret", "token", "api_key", "apikey"))]
    assert suspicious, "the guard itself must still match something"
    assert set(suspicious) <= set(testing_flags.SECRETS), suspicious


def test_the_flag_has_no_default_value():
    """The password flag has no baked-in default."""
    assert registry()["testing.kiwi_password"].default == ""


def test_the_detection_of_the_kernel_field_matches_the_dataclass():
    """`_SECRET_SUPPORTED` is a probe, not an assumption: passing an unsupported kwarg would TypeError at
    import time, and this module is imported by workflow discovery — so it would break every CLI
    invocation, not just a kiwi run."""
    assert SECRET_SUPPORTED == any(f.name == "secret" for f in dataclasses.fields(Flag))


@needs_secret_field
def test_the_registered_flag_carries_the_secret_marker():
    assert registry()["testing.kiwi_password"].secret is True


@needs_secret_field
def test_a_resolved_password_never_appears_in_the_flags_dump():
    """`rfc_agent/runner.py` writes `flags.dump()` to `<run_dir>/flags.json` before any stage runs, and
    workflow discovery registers these flags even for a plain `rfc-agent --prd ...` run."""
    flags = resolve(env={"TCMS_PASSWORD": "hunter2SECRET"})
    dumped = flags.dump()
    assert "hunter2SECRET" not in dumped
    assert "hunter2SECRET" not in str(flags.as_dict())


@needs_secret_field
def test_the_value_is_still_available_to_the_code_that_needs_it():
    """Redaction is for the serialised view only — the store must still be able to connect."""
    flags = resolve(env={"TCMS_PASSWORD": "hunter2SECRET"})
    assert flags.get("testing.kiwi_password") == "hunter2SECRET"


@needs_secret_field
def test_a_non_secret_flag_is_untouched():
    flags = resolve(env={"TCMS_USERNAME": "svc-qa"})
    assert "svc-qa" in flags.dump()


# ---- TLS scope --------------------------------------------------------------------------------------
class FakeTransport:
    context = None


class FakeProxy:
    def __init__(self):
        self._ServerProxy__transport = FakeTransport()


class FakeTCMS:
    """Stands in for tcms_api.TCMS: records how it was constructed and exposes a proxy-shaped transport."""

    last: FakeTCMS | None = None

    def __init__(self, *args):
        self.args = args
        self.exec = FakeProxy()
        self.default_at_connect = ssl._create_default_https_context
        FakeTCMS.last = self


def store(**kw) -> KiwiCaseStore:
    return KiwiCaseStore(url="https://kiwi.test/xml-rpc/", user="svc", password="s3cret", **kw)


def test_the_insecure_context_is_unverified():
    ctx = store(insecure=True)._tls_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_no_context_is_built_when_verification_stays_on():
    assert store(insecure=False)._tls_context() is None


def test_the_context_is_attached_to_the_transport():
    s = store(insecure=True)
    client = s._client(FakeTCMS, s._tls_context())
    assert isinstance(client.exec._ServerProxy__transport.context, ssl.SSLContext)
    assert client.exec._ServerProxy__transport.context.verify_mode == ssl.CERT_NONE
    assert s._context_attached is True


def test_the_process_wide_default_is_restored_after_connecting():
    """Connecting with `insecure=True` must not leave `ssl._create_default_https_context` pointing at the
    unverified context: otherwise every later HTTPS call in the process (Atlassian, Figma, an engine adapter)
    silently stops verifying certificates, for good."""
    before = ssl._create_default_https_context
    s = store(insecure=True)
    s._client(FakeTCMS, s._tls_context())
    assert ssl._create_default_https_context is before


def test_the_default_is_restored_even_when_the_connection_raises():
    class Exploding:
        def __init__(self, *a):
            raise ConnectionRefusedError("kiwi is down")

    before = ssl._create_default_https_context
    s = store(insecure=True)
    with pytest.raises(ConnectionRefusedError):
        s._client(Exploding, s._tls_context())
    assert ssl._create_default_https_context is before


def test_a_secure_connection_never_touches_the_global_default():
    before = ssl._create_default_https_context
    s = store(insecure=False)
    s._client(FakeTCMS, s._tls_context())
    assert ssl._create_default_https_context is before
    assert FakeTCMS.last.default_at_connect is before
    assert FakeTCMS.last.exec._ServerProxy__transport.context is None


def test_the_swap_covers_the_handshake_that_happens_during_construction():
    """tcms-api connects inside its own constructor, so the context has to be reachable then too — the
    window is that call, not the process's lifetime."""
    s = store(insecure=True)
    ctx = s._tls_context()
    s._client(FakeTCMS, ctx)
    assert FakeTCMS.last.default_at_connect() is ctx


def test_a_transport_with_no_context_slot_is_reported_not_crashed():
    """Which transport class tcms-api builds has moved between versions; failing to attach is a fallback,
    not an error."""
    class Bare:
        def __init__(self, *a):
            self.exec = object()

    s = store(insecure=True)
    s._client(Bare, s._tls_context())
    assert s._context_attached is False


def test_health_reports_the_tls_posture():
    """An operator reading `health()` must be able to see that verification is off."""
    rpc = type("R", (), {"User": type("U", (), {"filter": staticmethod(lambda q: [{"id": 1}])})()})()
    assert store(insecure=True, rpc=rpc).health()["tls_verified"] is False
    assert store(insecure=False, rpc=rpc).health()["tls_verified"] is True


def test_the_flag_help_says_it_is_insecure_and_how_to_stop_being_insecure():
    help_text = registry()["testing.insecure_tls"].help
    assert "INSECURE" in help_text
    assert "read and rewrite" in help_text          # states the actual consequence
    assert "trust the deployment's CA" in help_text  # states the remedy
    assert "--disable testing.insecure_tls" in help_text


def test_the_default_still_matches_todays_behaviour():
    """Parity: flipping it would break every existing Kiwi deployment silently."""
    assert registry()["testing.insecure_tls"].default is True
