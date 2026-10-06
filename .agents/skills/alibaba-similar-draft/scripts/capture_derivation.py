from __future__ import annotations

"""Deterministic derivation from raw browser field observations.

This module deliberately implements *consistency*, not provenance.  A local
file can be re-written by a local administrator and a Codex tool-result claim
is self-reported.  Consequently ``trust_status`` is always
``LOCAL_CONTRACT`` here.  A future ``LIVE_ATTESTED`` implementation must live
behind a separately reviewed external attestation verifier with its own trust
root; capture fields, adapter capabilities, and booleans must never promote
trust.
"""

import copy
import hashlib
import json
from typing import Any


LOCAL_TRUST_STATUS = "LOCAL_CONTRACT"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def object_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return False


def observation_material(observation: dict[str, Any]) -> dict[str, Any]:
    material = copy.deepcopy(observation)
    material.pop("observation_sha256", None)
    return material


def capture_body_material(capture: dict[str, Any]) -> dict[str, Any]:
    material = copy.deepcopy(capture)
    material.pop("capture_body_sha256", None)
    return material


def capture_trust_status(_capture: dict[str, Any]) -> str:
    """Return the highest trust this local implementation can establish.

    This function intentionally does not inspect ``source.kind`` and accepts
    no adapter or caller-supplied "trusted" flag.  Without an independently
    installed and reviewed external verifier, ``LIVE_ATTESTED`` is
    unreachable by construction.
    """

    return LOCAL_TRUST_STATUS


def validate_capture_integrity(capture: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    observations = capture.get("observations")
    if not isinstance(observations, list):
        return ["browser observation capture observations must be an array"]

    observed_ids: set[str] = set()
    observed_paths: set[str] = set()
    ordered_ids: list[str] = []
    for index, row in enumerate(observations):
        if not isinstance(row, dict):
            errors.append(f"browser observation[{index}] must be an object")
            continue
        observation_id = str(row.get("observation_id") or "")
        observation_type = str(row.get("observation_type") or "")
        path = str(row.get("path") or "")
        if observation_id in observed_ids:
            errors.append(f"duplicate browser observation id: {observation_id}")
        observed_ids.add(observation_id)
        ordered_ids.append(observation_id)
        if observation_type == "OFFICIAL_FIELD_VALUE":
            if path in observed_paths:
                errors.append(f"duplicate browser observation path: {path}")
            observed_paths.add(path)
        elif observation_type == "LOCALIZED_COUNTER":
            if row.get("browser_session_sha256") != capture.get("browser_session_sha256"):
                errors.append(f"localized counter session mismatch: {observation_id or index}")
            if row.get("page_fingerprint_sha256") != capture.get("page_fingerprint_sha256"):
                errors.append(f"localized counter page fingerprint mismatch: {observation_id or index}")

        declared = str(row.get("observation_sha256") or "").lower()
        computed = object_sha256(observation_material(row))
        if declared != computed:
            errors.append(f"browser observation hash mismatch: {observation_id or index}")

    if capture.get("observation_ids_sha256") != object_sha256(ordered_ids):
        errors.append("browser observation ids hash mismatch")

    declared_body = str(capture.get("capture_body_sha256") or "").lower()
    if declared_body != object_sha256(capture_body_material(capture)):
        errors.append("browser observation capture body hash mismatch")
    return sorted(set(errors))


def derive_current_field_rows(capture: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Derive every ledger field from observations; never trust ledger input."""

    errors = validate_capture_integrity(capture)
    rows: list[dict[str, Any]] = []
    for observation in capture.get("observations", []):
        if not isinstance(observation, dict):
            continue
        if observation.get("observation_type") != "OFFICIAL_FIELD_VALUE":
            continue
        value = copy.deepcopy(observation.get("value"))
        rows.append(
            {
                "path": observation.get("path"),
                "id": observation.get("id"),
                "value": value,
                "value_sha256": object_sha256(value),
                "value_present": value_present(value),
                "observed_disabled": observation.get("observed_disabled"),
                "write_targeted": observation.get("write_targeted"),
                "observation_id": observation.get("observation_id"),
                "observation_sha256": observation.get("observation_sha256"),
            }
        )
    return rows, errors


def derived_capture_bindings(capture: dict[str, Any], capture_file_sha256: str) -> dict[str, Any]:
    return {
        "observation_capture_id": capture.get("capture_id"),
        "observation_capture_sha256": capture_file_sha256,
        "observation_capture_body_sha256": capture.get("capture_body_sha256"),
        "observation_ids_sha256": capture.get("observation_ids_sha256"),
    }
