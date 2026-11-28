"""Unit tests for workforce governance runtime (offline, no MongoDB required)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

GATEWAY_ROOT = Path(__file__).resolve().parents[2] / "services" / "gateway"
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from app.lineage_envelope import LineageEnvelope  # noqa: E402
from app.policy_engine import _evaluate_rules  # noqa: E402
from app.workforce_common import (  # noqa: E402
    WORKFORCE_TYPES,
    assert_workforce_access,
    compute_inherited_roles,
    workforce_scope_filter,
)
from app.workforce_lifecycle import ALLOWED_TRANSITIONS  # noqa: E402

pytestmark = pytest.mark.e2e_unit


def test_lineage_envelope_required_fields():
    trace_id = "11111111-1111-4111-8111-111111111111"
    env = LineageEnvelope.from_request(correlation_id="cid-1", trace_id=trace_id)
    payload = env.to_dict()
    for field in (
        "origin_system",
        "owning_system",
        "schema_version",
        "trace_id",
        "correlation_id",
        "trust_classification",
        "visibility_scope",
    ):
        assert field in payload
    assert payload["trace_id"] == trace_id


def test_lineage_envelope_rejects_non_uuid_trace_and_never_copies_correlation():
    import pytest

    with pytest.raises(ValueError, match="UUIDv4"):
        LineageEnvelope.from_request(correlation_id="cid-1", trace_id="trace-1")

    env = LineageEnvelope.from_request(correlation_id="cid-1")
    assert env.trace_id != "cid-1"
    assert len(env.trace_id.split("-")) == 5
    assert env.trace_id[14] == "4"


def test_workforce_types_are_supported_set():
    assert WORKFORCE_TYPES == frozenset(
        {"contractor", "employee", "consultant", "advisor", "intern", "vendor_workforce"}
    )


def test_workforce_scope_filter_platform():
    assert workforce_scope_filter({"scope": "platform"}) == {}


def test_workforce_scope_filter_client():
    filt = workforce_scope_filter({"scope": "client", "user_id": "TECH001"})
    assert filt == {"tenant_id": "TECH001"}


def test_assert_workforce_access_candidate_denied():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        assert_workforce_access({"type": "jwt_token", "role": "candidate", "user_id": "c1"})
    assert exc.value.status_code == 403


def test_role_inheritance_chain():
    roles = compute_inherited_roles("lead", ["manager"], ["org_admin"])
    assert roles == ["org_admin", "manager", "lead"]


def test_leave_policy_denies_low_tenure():
    result = _evaluate_rules("leave_policy", {"min_tenure_days": 90, "effect": "observe"}, {"tenure_days": 10})
    assert result["decision"] == "deny"


def test_visibility_policy_scope_mismatch():
    result = _evaluate_rules(
        "visibility_policy",
        {"require_scope_match": True, "effect": "allow"},
        {"scope_match": False},
    )
    assert result["decision"] == "deny"


def test_lifecycle_onboarding_path():
    assert "onboarding" in ALLOWED_TRANSITIONS["draft"]
    assert "active" in ALLOWED_TRANSITIONS["onboarding"]
