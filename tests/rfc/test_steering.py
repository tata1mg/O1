"""Context-layer steering (v7 postmortem, 2026-08-10): conventions the composer would otherwise
invent. Accelerator contract: any fetch/parse failure yields NO steering, never a broken compose."""


from rfc_agent.steering import _distill

_FIXTURE = {
    "_meta": {"source_ref": "013338b4f752", "generated_at": "2026-07-24T08:05:28Z"},
    "coding_patterns": [
        {"pattern": "Layered controller/manager/repository", "description": "Thin routes delegate to managers.",
         "evidence": ["app/routes/svc_a/cart.py", "app/managers/cart.py"]},
    ],
    "shared_libraries": [
        {"name": "feature_flag_sdk", "type": "internal",
         "usage": "Feature flag init gated by is_feature_flagging_enabled."},
    ],
    "important_classes": [{"name": "APIClient", "path": "app/service_clients/base_api_client.py"}],
}


def test_distill_carries_idioms_and_provenance():
    block = _distill("svc_a", _FIXTURE)
    assert "is_feature_flagging_enabled" in block          # the v7 blocker idiom survives distillation
    assert "source_ref 013338b4" in block                  # staleness visible
    assert "Layered controller/manager" in block and "APIClient" in block


