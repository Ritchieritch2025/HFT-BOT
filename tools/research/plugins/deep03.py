"""Structured planning adapter for the existing Deep03 one-shot runner.

This module deliberately has no execution primitive.  It receives only a
validated job specification and data-selection metadata, then describes what
the separately authorized Deep03 gate would have to bind before a one-shot
run can start.
"""

from __future__ import annotations

from typing import Any


PLUGIN_DESCRIPTOR = {
    "schema_version": "research-method-plugin-v1",
    "plugin_id": "deep03",
    "plugin_version": "1",
    "payload_schema": "deep03-structured-execution-request-v1",
    "execution_class": "READONLY_EXPLORATORY",
}

METHOD_SCOPE = {
    "D3-B01-MARKOUT": "PARTIAL_DESCRIPTIVE_ONLY",
    "D3-B02-ONESIDE": "PARTIAL_DESCRIPTIVE_ONLY",
    "D3-B03-XMKT": "NOT_ESTIMABLE_PREFLIGHT_ONLY",
    "D3-B04-RHYTHM": "PARTIAL_DESCRIPTIVE_ONLY",
}


def build_planning_payload(context: dict[str, Any]) -> dict[str, Any]:
    """Return JSON data for a later authority-bound Deep03 invocation."""
    selection = context["data_selection"]
    preflight = context["preflight"]
    job_spec = context["job_spec"]
    return {
        "schema_version": PLUGIN_DESCRIPTOR["payload_schema"],
        "plugin_id": PLUGIN_DESCRIPTOR["plugin_id"],
        "plugin_version": PLUGIN_DESCRIPTOR["plugin_version"],
        "state": "AWAITING_EXISTING_AUTHORITY_AND_ONESHOT_GATE",
        "mode": "MODE 1 / EXPLORATORY_AUTORESEARCH",
        "phase": "OPEN_DISCOVERY",
        "work_package": "D3-W2A",
        "method_scope": dict(METHOD_SCOPE),
        "input_binding": {
            "selection_sha256": selection["selection_sha256"],
            "preflight_sha256": preflight["preflight_sha256"],
            "release_ids": list(selection["release_ids"]),
            "exact_version_required": True,
            "zero_copy": True,
        },
        "requested_report_sections": job_spec.get("report_requirements", []),
        "budget_ceiling": dict(job_spec["budget"]),
        "downstream_gate_contract": {
            "authority_schema": "deep03-w09-execution-authority-v1",
            "arm_schema": "deep03-w09-execution-arm-v1",
            "authority_gate_component": "deep03_authority_gate",
            "prepare_component": "deep03_v3_prepare",
            "runner_component": "deep03_v3_runner",
            "service_type": "SYSTEMD_ONESHOT",
            "must_bind_exact_release_ids": True,
            "must_validate_authority_and_arm": True,
            "must_reject_scope_broadening": True,
            "gate_evaluated_by_planning_worker": False,
        },
        "capabilities": {
            "direct_execution": False,
            "gate_bypass": False,
            "plan_markdown_execution": False,
            "arbitrary_code_execution": False,
            "network_write": False,
            "s3_write": False,
            "production_mutation": False,
            "trading": False,
            "rfq_included": False,
            "strict_acceptance_claim": False,
            "candidate_or_profit_claim": False,
        },
    }

