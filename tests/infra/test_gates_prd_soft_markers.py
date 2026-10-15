"""Ingest prd.md marker scan: soft-dependency fetch failures (instrumentation sheet as an
Office file) must not hard-fail the gate; a PRD-level auth wall still must."""



class _Ctx:
    def __init__(self, tmp):
        self.run_dir = tmp
        self.inputs = {}
    def path(self, rel):
        return self.run_dir / rel


def test_absence_regex_ignores_quoted_error_literals():
    # 2026-08-15 FP: 'raising `BadRequestException` on "not found"' is an error-message literal,
    # not an absence claim about the token
    import re

    from rfc_agent.gates import _ADJACENT_ABSENCE
    esc = re.escape("BadRequestException")
    fp = 'raising `BadRequestException` on "not found"/"not eligible".'
    real = 'grepped svc_a for `BadRequestException` — not found anywhere'
    assert not re.search(_ADJACENT_ABSENCE % esc, fp, re.IGNORECASE)
    assert re.search(_ADJACENT_ABSENCE % esc, real, re.IGNORECASE)
