"""Conformance checks for the server-owned protocol-v1 contract.

These tests use the runtime-pinned IDNA implementation and a test-only cryptography
library to validate public P-256 signatures. They provide no production
cryptographic primitive or network listener.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import socket
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, Final, NamedTuple
from urllib.parse import parse_qs, urlsplit

import idna
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

ROOT: Final = Path(__file__).parents[2]
PROFILE: Final = ROOT / "docs" / "protocol" / "v1"
SCHEMAS: Final = PROFILE / "schemas"
VECTORS: Final = PROFILE / "vectors"

EXPECTED_PROFILE_FILES: Final = {
    "README.md",
    "threat-model.md",
    "wire-profile.md",
    "routes.json",
    "schemas/common.schema.json",
    "schemas/problem.schema.json",
    "schemas/reporter-api.schema.json",
    "schemas/control-api.schema.json",
    "schemas/mqtt-v2.schema.json",
    "schemas/vector.schema.json",
    "vectors/manifest.json",
    "vectors/common.json",
    "vectors/http-signatures.json",
    "vectors/interactive-enrollment.json",
    "vectors/managed-enrollment.json",
    "vectors/control-bootstrap-recovery.json",
    "vectors/key-rotations.json",
    "vectors/managed-token-control.json",
    "vectors/mqtt-v2-aead.json",
}

REPORTER_ROUTES: Final = {
    ("POST", "/api/v1/managed-enrollment-challenges"),
    ("POST", "/api/v1/enrollments"),
    ("POST", "/api/v1/enrollments/{enrollment_id}/reveal"),
    ("POST", "/api/v1/enrollments/{enrollment_id}/confirm"),
    ("GET", "/api/v1/enrollments/{enrollment_id}"),
    ("GET", "/api/v1/challenge-context"),
    ("POST", "/api/v1/challenges"),
    ("POST", "/api/v1/sessions"),
    ("POST", "/api/v1/reports"),
    ("POST", "/api/v1/reporters/{reporter_id}/key-rotations/challenges"),
    ("POST", "/api/v1/reporters/{reporter_id}/key-rotations/{rotation_id}"),
}
CONTROL_ROUTES: Final = {
    ("POST", "/control/v1/bootstrap/commit"),
    ("POST", "/control/v1/bootstrap/reveal"),
    ("POST", "/control/v1/bootstrap/confirm"),
    ("POST", "/control/v1/recovery/commit"),
    ("POST", "/control/v1/recovery/reveal"),
    ("POST", "/control/v1/recovery/revoke"),
    ("POST", "/control/v1/managed-enrollment-tokens"),
    ("POST", "/control/v1/managed-enrollment-token-operations/status"),
    ("POST", "/control/v1/managed-enrollment-tokens/revoke"),
    ("GET", "/control/v1/roster/events"),
    ("GET", "/control/v1/roster/snapshot"),
    ("POST", "/control/v1/reporters/actions"),
    ("POST", "/control/v1/reporters/key-rotations/confirm"),
    ("POST", "/control/v1/control-keys/rotation-challenges"),
    ("POST", "/control/v1/control-keys/rotations"),
    ("POST", "/control/v1/control-spki/rotations"),
    ("POST", "/control/v1/mqtt-keys/provision"),
    ("POST", "/control/v1/mqtt-keys/acknowledge"),
    ("POST", "/control/v1/leases/refresh"),
}


class RouteLimits(NamedTuple):
    """One route's request, response, and successful-list limits."""

    request_body_maximum: int
    success_body_maximum: int
    success_status: int
    success_item_maximum: int | None


class SignatureFixture(NamedTuple):
    """Locations of one positive signature, high-S twin, key, and message."""

    case_id: str
    positive_group: str
    positive_name: str
    high_s_name: str
    key_group: str
    key_name: str
    message_group: str
    message_name: str


P256_ORDER: Final = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
PUBLISHED_SIGNATURE_FIXTURES: Final = (
    SignatureFixture(
        case_id="managed.proof",
        positive_group="expected",
        positive_name="proposed_signature",
        high_s_name="proposed_signature.high-s",
        key_group="inputs",
        key_name="proposed_jwk",
        message_group="intermediates",
        message_name="transcript",
    ),
    SignatureFixture(
        case_id="rotation.reporter",
        positive_group="expected",
        positive_name="current_signature",
        high_s_name="current_signature.high-s",
        key_group="inputs",
        key_name="current_jwk",
        message_group="intermediates",
        message_name="transcript",
    ),
    SignatureFixture(
        case_id="rotation.reporter",
        positive_group="expected",
        positive_name="proposed_signature",
        high_s_name="proposed_signature.high-s",
        key_group="inputs",
        key_name="proposed_jwk",
        message_group="intermediates",
        message_name="transcript",
    ),
    SignatureFixture(
        case_id="rotation.control-key",
        positive_group="intermediates",
        positive_name="signature.rem.p1363",
        high_s_name="signature.rem.high-s",
        key_group="inputs",
        key_name="jwk.rem",
        message_group="intermediates",
        message_name="signature-base.rem",
    ),
    SignatureFixture(
        case_id="rotation.control-key",
        positive_group="intermediates",
        positive_name="signature.rem-proposed.p1363",
        high_s_name="signature.rem-proposed.high-s",
        key_group="inputs",
        key_name="jwk.rem-proposed",
        message_group="intermediates",
        message_name="signature-base.rem-proposed",
    ),
    SignatureFixture(
        case_id="rotation.control-spki",
        positive_group="expected",
        positive_name="old_spki_signature",
        high_s_name="old_spki_signature.high-s",
        key_group="inputs",
        key_name="old_spki_der",
        message_group="intermediates",
        message_name="transcript",
    ),
    SignatureFixture(
        case_id="rotation.control-spki",
        positive_group="expected",
        positive_name="new_spki_signature",
        high_s_name="new_spki_signature.high-s",
        key_group="inputs",
        key_name="new_spki_der",
        message_group="intermediates",
        message_name="transcript",
    ),
)


EXPECTED_ROUTE_LIMITS: Final = {
    "reporter.managed-enrollment-challenge.create": RouteLimits(1024, 1024, 201, None),
    "reporter.enrollment.create": RouteLimits(4096, 2048, 201, None),
    "reporter.enrollment.reveal": RouteLimits(256, 2048, 200, None),
    "reporter.enrollment.confirm": RouteLimits(512, 1024, 200, None),
    "reporter.enrollment.status": RouteLimits(0, 2048, 200, None),
    "reporter.challenge-context.read": RouteLimits(0, 1024, 200, None),
    "reporter.challenge.create": RouteLimits(512, 1024, 201, None),
    "reporter.session.create": RouteLimits(1024, 1024, 201, None),
    "reporter.report.create": RouteLimits(1024, 1024, 200, None),
    "reporter.key-rotation-challenge.create": RouteLimits(2048, 2048, 201, None),
    "reporter.key-rotation.complete": RouteLimits(1024, 2048, 202, None),
    "control.bootstrap.commit": RouteLimits(2048, 2048, 201, None),
    "control.bootstrap.reveal": RouteLimits(1024, 2048, 200, None),
    "control.bootstrap.confirm": RouteLimits(1024, 2048, 200, None),
    "control.recovery.commit": RouteLimits(1024, 2048, 201, None),
    "control.recovery.reveal": RouteLimits(256, 1024, 200, None),
    "control.recovery.revoke": RouteLimits(1024, 2048, 200, None),
    "control.managed-token.issue": RouteLimits(1024, 2048, 201, None),
    "control.managed-token.status": RouteLimits(512, 1024, 200, None),
    "control.managed-token.revoke": RouteLimits(512, 1024, 200, None),
    "control.roster.events": RouteLimits(0, 65536, 200, 100),
    "control.roster.snapshot": RouteLimits(0, 65536, 200, 100),
    "control.reporter.action": RouteLimits(2048, 2048, 200, None),
    "control.reporter-key-rotation.confirm": RouteLimits(1024, 2048, 200, None),
    "control.control-key-rotation-challenge.create": RouteLimits(2048, 2048, 201, None),
    "control.control-key-rotation.complete": RouteLimits(4096, 2048, 200, None),
    "control.control-spki-rotation": RouteLimits(4096, 2048, 200, None),
    "control.mqtt-key.provision": RouteLimits(4096, 4096, 200, None),
    "control.mqtt-key.acknowledge": RouteLimits(1024, 2048, 200, None),
    "control.lease.refresh": RouteLimits(512, 1024, 200, None),
}
EXPECTED_GLOBAL_LIMITS: Final = {
    "request_target_max_bytes": 256,
    "header_count_max": 24,
    "header_bytes_max": 8192,
    "signature_header_max_bytes": 1024,
    "signature_input_header_max_bytes": 1024,
    "content_digest_header_max_bytes": 256,
    "problem_response_max_bytes": 2048,
}
ERROR_CODES: Final = {
    "invalid_request": 400,
    "authentication_required": 401,
    "authorization_denied": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "request_timeout": 408,
    "conflict": 409,
    "snapshot_required": 409,
    "stale_snapshot": 410,
    "operation_result_expired": 410,
    "request_body_too_large": 413,
    "target_too_long": 414,
    "unsupported_media_type": 415,
    "rate_limited": 429,
    "headers_too_large": 431,
    "internal_error": 500,
    "overloaded": 503,
}
VECTOR_FILES: Final = {
    "common.json",
    "http-signatures.json",
    "interactive-enrollment.json",
    "managed-enrollment.json",
    "control-bootstrap-recovery.json",
    "key-rotations.json",
    "managed-token-control.json",
    "mqtt-v2-aead.json",
}
REQUIRED_VECTOR_IDS: Final = {
    "common.transcript-encoding",
    "common.jwk-thumbprint",
    "common.content-digest",
    "http.reporter-signature",
    "http.control-signature",
    "interactive.commitments",
    "interactive.sas",
    "managed.proof",
    "bootstrap.commit-reveal-confirm",
    "recovery.commit-reveal-revoke",
    "rotation.reporter",
    "rotation.control-key",
    "rotation.control-spki",
    "managed-token.issue-status-revoke",
    "mqtt.key-provision",
    "mqtt.key-acknowledge",
    "mqtt.aead-envelope",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _files_below(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _walk_json_objects(value: Any, location: str = "$") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        yield location, value
        for key, child in value.items():
            yield from _walk_json_objects(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json_objects(child, f"{location}[{index}]")


_SCHEMA_NAMESPACE: Final = "https://remote-energy-meter.example/protocol/v1/schemas/"


def _resolve_schema_ref(reference: str, current_document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if reference.startswith("#"):
        document = current_document
        fragment = reference[1:]
    else:
        assert reference.startswith(_SCHEMA_NAMESPACE)
        target_uri, fragment = reference.split("#", 1)
        document = _load(SCHEMAS / target_uri.removeprefix(_SCHEMA_NAMESPACE))
    target: Any = document
    for part in fragment.strip("/").split("/"):
        if part:
            target = target[part.replace("~1", "/").replace("~0", "~")]
    assert isinstance(target, dict)
    return target, document


def _schema_accepts(instance: Any, schema: dict[str, Any], document: dict[str, Any]) -> bool:
    try:
        _assert_schema_accepts(instance, schema, document)
    except AssertionError:
        return False
    return True


def _assert_schema_combinators(instance: Any, schema: dict[str, Any], document: dict[str, Any]) -> None:
    if "allOf" in schema:
        for branch in schema["allOf"]:
            _assert_schema_accepts(instance, branch, document)
    if "oneOf" in schema:
        matches = sum(_schema_accepts(instance, branch, document) for branch in schema["oneOf"])
        assert matches == 1
    if "not" in schema:
        assert not _schema_accepts(instance, schema["not"], document)
    if "if" in schema:
        branch_name = "then" if _schema_accepts(instance, schema["if"], document) else "else"
        if branch_name in schema:
            _assert_schema_accepts(instance, schema[branch_name], document)


def _assert_schema_type(instance: Any, expected_type: str | None) -> None:
    if expected_type == "null":
        assert instance is None
    elif expected_type == "boolean":
        assert isinstance(instance, bool)
    elif expected_type == "integer":
        assert isinstance(instance, int) and not isinstance(instance, bool)
    elif expected_type == "number":
        assert isinstance(instance, int | float) and not isinstance(instance, bool)
    elif expected_type == "string":
        assert isinstance(instance, str)
    elif expected_type == "array":
        assert isinstance(instance, list)
    elif expected_type == "object":
        assert isinstance(instance, dict)


def _assert_schema_scalar_constraints(instance: Any, schema: dict[str, Any]) -> None:
    if "const" in schema:
        assert instance == schema["const"] and type(instance) is type(schema["const"])
    if "enum" in schema:
        assert any(instance == value and type(instance) is type(value) for value in schema["enum"])

    if isinstance(instance, str):
        assert len(instance) >= schema.get("minLength", 0)
        assert len(instance) <= schema.get("maxLength", len(instance))
        if "pattern" in schema:
            assert re.search(schema["pattern"], instance)
        semantic_validation = schema.get("x-rem-semantic-validation")
        if semantic_validation == "rem-idna2008-alabel-host-v1":
            assert _is_canonical_dns_host(instance)
        elif semantic_validation == "rem-server-identity-v1":
            authority = instance.removeprefix("https://")
            host, separator, port = authority.rpartition(":")
            assert separator and port and _is_canonical_dns_host(host)
        elif semantic_validation == "rem-rfc3339-utc-gregorian-v1":
            try:
                date(int(instance[0:4]), int(instance[5:7]), int(instance[8:10]))
            except ValueError as error:
                raise AssertionError("invalid Gregorian UTC date") from error
        else:
            assert semantic_validation is None
    if isinstance(instance, int | float) and not isinstance(instance, bool):
        assert instance >= schema.get("minimum", instance)
        assert instance <= schema.get("maximum", instance)


def _assert_schema_collection_constraints(instance: Any, schema: dict[str, Any], document: dict[str, Any]) -> None:
    if isinstance(instance, list):
        assert len(instance) >= schema.get("minItems", 0)
        assert len(instance) <= schema.get("maxItems", len(instance))
        if "items" in schema:
            for item in instance:
                _assert_schema_accepts(item, schema["items"], document)
    if isinstance(instance, dict):
        assert set(schema.get("required", ())) <= set(instance)
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            assert set(instance) <= set(properties)
        for key, child_schema in properties.items():
            if key in instance:
                _assert_schema_accepts(instance[key], child_schema, document)


def _assert_schema_accepts(instance: Any, schema: dict[str, Any], document: dict[str, Any]) -> None:
    """Evaluate the dependency-free JSON Schema subset used by the protocol artifacts."""
    if "$ref" in schema:
        target, target_document = _resolve_schema_ref(schema["$ref"], document)
        _assert_schema_accepts(instance, target, target_document)

    _assert_schema_combinators(instance, schema, document)
    _assert_schema_type(instance, schema.get("type"))
    _assert_schema_scalar_constraints(instance, schema)
    _assert_schema_collection_constraints(instance, schema, document)


def _named_value(case: dict[str, Any], group: str, name: str) -> dict[str, str]:
    values = case["expected"]["outputs"] if group == "expected" else case[group]
    return next(value for value in values if value["name"] == name)


def _decode_named_value(value: dict[str, str]) -> bytes:
    if value["encoding"] == "hex":
        return bytes.fromhex(value["value"])
    assert value["encoding"] == "base64url"
    text = value["value"]
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _is_canonical_dns_host(host: str) -> bool:
    """Apply deterministic syntax, legacy-IPv4, and strict IDNA2008 checks."""
    if not host or host != host.lower() or host.endswith(".") or any(ord(character) > 127 for character in host):
        return False
    labels = host.split(".")
    label_pattern = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
    if not all(label_pattern.fullmatch(label) for label in labels):
        return False
    try:
        socket.inet_aton(host)
    except OSError:
        pass
    else:
        return False
    try:
        for label in labels:
            decoded = idna.decode(label, strict=True, uts46=True, std3_rules=True)
            canonical = idna.encode(
                decoded,
                strict=True,
                uts46=True,
                std3_rules=True,
                transitional=False,
            ).decode("ascii")
            if canonical != label:
                return False
    except idna.IDNAError:
        return False
    return True


def _assert_canonical_named_value(value: dict[str, str]) -> None:
    encoding = value["encoding"]
    text = value["value"]
    if encoding == "ascii":
        text.encode("ascii")
    elif encoding == "utf8":
        text.encode("utf-8")
    elif encoding == "hex":
        assert len(text) % 2 == 0 and text == text.lower()
        bytes.fromhex(text)
    elif encoding == "base64url":
        assert "=" not in text and re.fullmatch(r"[A-Za-z0-9_-]*", text)
        decoded = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        assert base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() == text
    elif encoding == "base64":
        try:
            decoded = base64.b64decode(text, validate=True)
        except binascii.Error as error:
            raise AssertionError(f"invalid base64 value {value['name']}") from error
        assert base64.b64encode(decoded).decode() == text
    elif encoding == "decimal":
        assert text == str(int(text)) and not text.startswith("-")
    else:
        assert encoding == "sas-placeholder"
        assert re.fullmatch(r"EVEN\[\d{1,3}\](?: ODD\[\d{1,3}\] EVEN\[\d{1,3}\]){2} ODD\[\d{1,3}\]", text)


def test_protocol_profile_has_the_exact_versioned_file_set() -> None:
    assert _files_below(PROFILE) == EXPECTED_PROFILE_FILES


def test_route_manifest_is_complete_bounded_and_unambiguous() -> None:
    manifest = _load(PROFILE / "routes.json")
    assert set(manifest) == {"schema", "profile_version", "limits", "routes"}
    assert manifest["schema"] == "rem-route-manifest-v1"
    assert manifest["profile_version"] == 1
    assert manifest["limits"] == EXPECTED_GLOBAL_LIMITS
    routes = manifest["routes"]
    assert len({route["id"] for route in routes}) == len(routes)
    assert {route["id"] for route in routes} == set(EXPECTED_ROUTE_LIMITS)
    assert {(r["method"], r["path_template"]) for r in routes if r["surface"] == "reporter"} == REPORTER_ROUTES
    assert {(r["method"], r["path_template"]) for r in routes if r["surface"] == "control"} == CONTROL_ROUTES

    for route in routes:
        assert set(route) == {
            "id",
            "surface",
            "trust_state",
            "method",
            "path_template",
            "query_forms",
            "request_body_max_bytes",
            "success_body_max_bytes",
            "success_item_maximum",
            "success_status",
            "purpose",
            "request_schema",
            "response_schema",
            "signature_profile",
            "replay_rule",
        }
        actual_limits = RouteLimits(
            request_body_maximum=route["request_body_max_bytes"],
            success_body_maximum=route["success_body_max_bytes"],
            success_status=route["success_status"],
            success_item_maximum=route["success_item_maximum"],
        )
        assert actual_limits == EXPECTED_ROUTE_LIMITS[route["id"]]
        assert route["method"] in {"GET", "POST"}
        assert route["purpose"] and route["replay_rule"]
        if route["method"] == "GET":
            assert route["request_body_max_bytes"] == 0
        if route["trust_state"] == "post-pair":
            assert route["signature_profile"] == "control-rfc9421-v1"
        if route["surface"] == "reporter" and route["signature_profile"] is not None:
            assert route["signature_profile"] == "reporter-rfc9421-v1"

    provisional = {r["id"] for r in routes if r["trust_state"] == "provisional-control"}
    assert provisional == {
        "control.bootstrap.commit",
        "control.bootstrap.reveal",
        "control.bootstrap.confirm",
        "control.recovery.commit",
        "control.recovery.reveal",
        "control.recovery.revoke",
    }


def test_every_route_points_to_existing_schema_definitions() -> None:
    routes = _load(PROFILE / "routes.json")["routes"]
    documents = {
        "reporter": _load(SCHEMAS / "reporter-api.schema.json"),
        "control": _load(SCHEMAS / "control-api.schema.json"),
    }
    for route in routes:
        document = documents[route["surface"]]
        for field in ("request_schema", "response_schema"):
            reference = route[field]
            if reference is None:
                assert route["request_body_max_bytes"] == 0 and field == "request_schema"
                continue
            prefix, name = reference.split("#/$defs/", 1)
            assert prefix.endswith(f"{route['surface']}-api.schema.json")
            assert name in document["$defs"], (route["id"], field, name)


def test_all_object_schemas_are_closed_and_require_every_property() -> None:
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        document = _load(path)
        assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert document["$id"].startswith("https://remote-energy-meter.example/protocol/v1/schemas/")
        for location, schema in _walk_json_objects(document):
            if schema.get("type") != "object" or "properties" not in schema:
                continue
            assert schema.get("additionalProperties") is False, f"open object at {path.name}:{location}"
            assert set(schema.get("required", [])) == set(schema["properties"]), (
                f"optional/undeclared property at {path.name}:{location}"
            )


def test_all_schema_references_resolve_inside_the_versioned_profile() -> None:
    namespace = "https://remote-energy-meter.example/protocol/v1/schemas/"
    documents = {path.name: _load(path) for path in SCHEMAS.glob("*.schema.json")}
    for source_name, document in documents.items():
        for location, schema in _walk_json_objects(document):
            reference = schema.get("$ref")
            if reference is None:
                continue
            if reference.startswith("#"):
                target = document
                fragment = reference[1:]
            else:
                assert reference.startswith(namespace), f"external reference at {source_name}:{location}"
                target_uri, fragment = reference.split("#", 1)
                target = documents[target_uri.removeprefix(namespace)]
            for part in fragment.strip("/").split("/"):
                target = target[part]


def test_problem_profile_has_one_closed_code_status_mapping() -> None:
    document = _load(SCHEMAS / "problem.schema.json")
    assert document["x-rem-code-status"] == ERROR_CODES
    problem = document["$defs"]["problem"]
    assert problem["properties"]["code"]["enum"] == list(ERROR_CODES)
    assert problem["properties"]["status"]["enum"] == sorted(set(ERROR_CODES.values()))
    assert problem["additionalProperties"] is False
    assert problem["required"] == ["type", "title", "status", "code"]
    assert problem["x-rem-complete-response-max-bytes"] == 2048


def test_vector_manifest_checks_exact_files_checksums_and_unique_ids() -> None:
    manifest = _load(VECTORS / "manifest.json")
    assert manifest["schema"] == "rem-vector-manifest-v1"
    assert manifest["profile_version"] == 1
    assert manifest["source_of_truth"] == "server-owned"
    assert {entry["file"] for entry in manifest["files"]} == VECTOR_FILES
    assert len({entry["file"] for entry in manifest["files"]}) == len(manifest["files"])

    all_ids: list[str] = []
    for entry in manifest["files"]:
        path = VECTORS / entry["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        vector = _load(path)
        assert vector["schema"] == "rem-vector-set-v1"
        assert vector["profile_version"] == 1
        case_ids = [case["id"] for case in vector["cases"]]
        assert entry["case_ids"] == case_ids
        all_ids.extend(case_ids)
    assert len(all_ids) == len(set(all_ids))
    assert set(all_ids) >= REQUIRED_VECTOR_IDS

    contract_paths = [entry["path"] for entry in manifest["contract_files"]]
    assert len(contract_paths) == len(set(contract_paths))
    assert set(contract_paths) == {
        "routes.json",
        "schemas/common.schema.json",
        "schemas/problem.schema.json",
        "schemas/reporter-api.schema.json",
        "schemas/control-api.schema.json",
        "schemas/mqtt-v2.schema.json",
        "schemas/vector.schema.json",
        "wire-profile.md",
    }
    for entry in manifest["contract_files"]:
        assert hashlib.sha256((PROFILE / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]


def test_every_vector_has_intermediate_bytes_and_independent_mutations() -> None:
    mutation_ids: list[str] = []
    for path in sorted(VECTORS.glob("*.json")):
        if path.name == "manifest.json":
            continue
        vector = _load(path)
        for case in vector["cases"]:
            assert case["expected"]["accept"] is True
            assert case["intermediates"], case["id"]
            assert case["mutations"], case["id"]
            value_groups = (case["inputs"], case["intermediates"], case["expected"]["outputs"])
            for values in value_groups:
                names = [value["name"] for value in values]
                assert len(names) == len(set(names)), case["id"]
                for value in values:
                    _assert_canonical_named_value(value)
            for substitute in case["substitutes"]:
                _assert_canonical_named_value(substitute["replacement"])
            fields = [mutation["field"] for mutation in case["mutations"]]
            assert len(fields) == len(set(fields)), case["id"]
            substitutes = {item["field"] for item in case["substitutes"]}
            for mutation in case["mutations"]:
                mutation_ids.append(mutation["id"])
                assert mutation["id"].startswith(f"{case['id']}.m")
                assert set(mutation) == {"id", "field", "operation", "expected_code"}
                assert mutation["expected_code"] in ERROR_CODES
                assert mutation["operation"] in {
                    "alter-byte",
                    "alter-value",
                    "duplicate",
                    "omit",
                    "reorder",
                    "substitute",
                }
                if mutation["operation"] == "substitute":
                    assert mutation["field"] in substitutes
    assert len(mutation_ids) == len(set(mutation_ids))


PRIVATE_KEY_NAMES: Final = frozenset({"d", "private_jwk", "private_key", "signing_key", "private_key_hex"})
PUBLIC_EC_JWK_MEMBERS: Final = frozenset({"crv", "kty", "x", "y"})


def _case_named_values(case: dict[str, Any]) -> Iterator[tuple[str, dict[str, str]]]:
    groups = {
        "inputs": case["inputs"],
        "intermediates": case["intermediates"],
        "expected.outputs": case["expected"]["outputs"],
        "substitutes": [substitute["replacement"] for substitute in case["substitutes"]],
    }
    for group, values in groups.items():
        for index, value in enumerate(values):
            yield f"{case['id']}.{group}[{index}]", value


def _private_material_in_json(document: Any, location: str) -> Iterator[str]:
    """Yield locations where a JSON document carries a private key member or a non-public JWK."""
    for path, value in _walk_json_objects(document, location):
        if not PRIVATE_KEY_NAMES.isdisjoint(value):
            yield f"private key member at {path}"
        if "kty" in value and set(value) != PUBLIC_EC_JWK_MEMBERS:
            yield f"JWK with members {sorted(value)} at {path}"


def _private_material_in_vector_set(document: Any, source: str) -> list[str]:
    """Return every place a vector set exposes private signing material, structurally or embedded."""
    findings = list(_private_material_in_json(document, source))
    for case in document["cases"]:
        for location, value in _case_named_values(case):
            name = value["name"]
            if name in PRIVATE_KEY_NAMES or name.rsplit(".", 1)[-1] in PRIVATE_KEY_NAMES:
                findings.append(f"private key value name {name!r} at {source}:{location}")
            if value["encoding"] not in {"utf8", "ascii"}:
                continue
            try:
                embedded = json.loads(value["value"])
            except json.JSONDecodeError:
                continue
            findings.extend(_private_material_in_json(embedded, f"{source}:{location}<{name}>"))
    return findings


def test_public_vectors_contain_no_private_signing_key() -> None:
    for path in sorted(VECTORS.glob("*.json")):
        document = _load(path)
        if path.name == "manifest.json":
            assert list(_private_material_in_json(document, path.name)) == []
            continue
        assert _private_material_in_vector_set(document, path.name) == []
    readme = (PROFILE / "README.md").read_text(encoding="utf-8")
    assert "public, non-secret fixtures" in readme
    assert "publish no private signing-key members" in readme


def test_profile_documents_fix_required_wire_rules_and_provenance() -> None:
    profile = (PROFILE / "wire-profile.md").read_text(encoding="utf-8")
    threat_model = (PROFILE / "threat-model.md").read_text(encoding="utf-8")
    required_profile_terms = {
        "REM-TRANSCRIPT",
        "RFC 7638",
        "RFC 9421",
        "RFC 9457",
        "RFC 9530",
        "Content-Digest",
        "@signature-params",
        "ecdsa-p256-sha256",
        "IEEE P1363",
        "low-S",
        "XChaCha20-Poly1305",
        "rem-control-spki-rotation-v1",
        "rem-mqtt-key-provision-v1",
        "PGP Word List",
        "provenance",
    }
    assert all(term in profile for term in required_profile_terms)
    assert "network location grants no trust" in threat_model.lower()
    assert "server receipt time" in threat_model.lower()
    assert "no listener" in threat_model.lower()


def test_key_provenance_is_documented_as_an_unverifiable_maintainer_claim() -> None:
    # Normalize whitespace: the Markdown wraps sentences across lines.
    readme = " ".join((PROFILE / "README.md").read_text(encoding="utf-8").split())
    threat_model = " ".join((PROFILE / "threat-model.md").read_text(encoding="utf-8").split())
    # The discard statement must stay attributed to the maintainer, never stated as a verified fact.
    assert "The maintainer's provenance is that seven ephemeral P-256 key pairs were generated" in readme
    assert "cannot prove how the keys were generated or that no private copy was retained elsewhere" in readme
    assert "must not treat this provenance statement as key-destruction evidence" in readme
    assert "The maintainer reports that the private halves were discarded" in threat_model
    assert "cannot prove private-key destruction" in threat_model
    # Negative guard: any sentence saying private material was discarded/destroyed must attribute it.
    for name in ("README.md", "threat-model.md", "wire-profile.md"):
        text = " ".join((PROFILE / name).read_text(encoding="utf-8").split())
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if "private" in sentence and re.search(r"\b(?:discarded|destroyed|deleted|erased|wiped)\b", sentence):
                assert "maintainer" in sentence, f"unattributed key-destruction claim in {name}: {sentence!r}"


def test_published_manifests_vectors_and_http_examples_conform_to_their_schemas() -> None:
    common_schema = _load(SCHEMAS / "common.schema.json")
    vector_schema = _load(SCHEMAS / "vector.schema.json")
    routes_document = _load(PROFILE / "routes.json")
    _assert_schema_accepts(routes_document, common_schema["$defs"]["routeManifest"], common_schema)
    _assert_schema_accepts(_load(VECTORS / "manifest.json"), vector_schema["$defs"]["vectorManifest"], vector_schema)

    for path in sorted(VECTORS.glob("*.json")):
        if path.name != "manifest.json":
            _assert_schema_accepts(_load(path), vector_schema["$defs"]["vectorSet"], vector_schema)

    routes = {route["id"]: route for route in routes_document["routes"]}
    http_vectors = _load(VECTORS / "http-signatures.json")
    for case in http_vectors["cases"]:
        assert len(case["routes"]) == 1, case["id"]
        route = routes[case["routes"][0]]
        inputs = {value["name"]: value for value in case["inputs"]}
        outputs = {value["name"]: value for value in case["expected"]["outputs"]}
        assert case["expected"]["status"] == route["success_status"], case["id"]

        if route["request_schema"] is None:
            assert "body" not in inputs, case["id"]
        else:
            body = inputs["body"]["value"]
            assert len(body.encode()) <= route["request_body_max_bytes"], case["id"]
            reference = _SCHEMA_NAMESPACE + route["request_schema"].removeprefix("schemas/")
            request_schema, request_document = _resolve_schema_ref(reference, common_schema)
            _assert_schema_accepts(json.loads(body), request_schema, request_document)

        response = outputs["response.body"]["value"]
        assert len(response.encode()) <= route["success_body_max_bytes"], case["id"]
        reference = _SCHEMA_NAMESPACE + route["response_schema"].removeprefix("schemas/")
        response_schema, response_document = _resolve_schema_ref(reference, common_schema)
        _assert_schema_accepts(json.loads(response), response_schema, response_document)


def test_interactive_enrollment_response_binds_trust_mode_to_trust_anchor() -> None:
    document = _load(SCHEMAS / "reporter-api.schema.json")
    schema = document["$defs"]["interactiveEnrollmentResponse"]
    response = {
        "core_commitment": "paWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaU",
        "enrollment_id": "AQEBAQEBAQEBAQEBAQEBAQ",
        "expires_at": 1767226200,
        "instance_id": "instance-a",
        "mode": "interactive",
        "reporter_commitment": "z9eKUQtsUV9F4SlCmY28nfxQfSwlMhHVQ2Ybz9pEec4",
        "schema_version": 1,
        "server_identity": "https://ha.example.com:58443",
        "trust_anchor_spki_sha256": "paWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaU",
        "trust_mode": "ca-spki-sha256",
    }
    platform_pki = {**response, "trust_anchor_spki_sha256": None, "trust_mode": "platform-pki"}
    assert _schema_accepts(response, schema, document)
    assert _schema_accepts(platform_pki, schema, document)
    assert not _schema_accepts({**response, "trust_anchor_spki_sha256": None}, schema, document)
    assert not _schema_accepts(
        {**platform_pki, "trust_anchor_spki_sha256": response["trust_anchor_spki_sha256"]}, schema, document
    )


def test_dns_host_and_server_identity_schemas_reject_alternate_and_noncanonical_forms() -> None:
    document = _load(SCHEMAS / "common.schema.json")
    dns_schema = document["$defs"]["dnsHost"]
    identity_schema = document["$defs"]["serverIdentity"]
    valid_hosts = {"device.example", "xn--caf-dma.example", "xn--fa-hia.example"}
    rejected_hosts = {
        "0x7f000001",
        "0177.0.0.1",
        "127.1",
        "[2001:db8::1]",
        "xn--7a.example",
        "xn--a.example",
        "xn--abc.example",
        "Device.example",
        "café.example",
        "device.example.",
        "device..example",
    }
    assert all(_is_canonical_dns_host(host) for host in valid_hosts)
    assert not any(_is_canonical_dns_host(host) for host in rejected_hosts)
    for host in valid_hosts:
        assert _schema_accepts(host, dns_schema, document), host
        assert _schema_accepts(f"https://{host}:8443", identity_schema, document), host
    schema_accepted_hosts = {host for host in rejected_hosts if _schema_accepts(host, dns_schema, document)}
    schema_accepted_identities = {
        host for host in rejected_hosts if _schema_accepts(f"https://{host}:8443", identity_schema, document)
    }
    assert schema_accepted_hosts == set()
    assert schema_accepted_identities == set()

    rejected_identities = {
        "http://device.example:8443",
        "https://device.example",
        "https://device.example:08443",
        "https://device.example:0",
        "https://device.example:65536",
        "https://user@device.example:8443",
        "https://device.example:8443/",
        "https://device.example:8443?query",
        "https://device.example:8443#fragment",
    }
    for identity in rejected_identities:
        assert not _schema_accepts(identity, identity_schema, document), identity


def _load_signature_public_key(
    case: dict[str, Any], fixture: SignatureFixture
) -> tuple[ec.EllipticCurvePublicKey, bytes | None]:
    key_value = _named_value(case, fixture.key_group, fixture.key_name)
    if fixture.key_name.endswith("spki_der"):
        spki_der = _decode_named_value(key_value)
        digest_name = fixture.key_name.removesuffix("_der") + "_sha256"
        expected_digest = _decode_named_value(_named_value(case, "intermediates", digest_name))
        assert hashlib.sha256(spki_der).digest() == expected_digest
        public_key = serialization.load_der_public_key(spki_der)
        assert isinstance(public_key, ec.EllipticCurvePublicKey)
    else:
        spki_der = None
        assert key_value["encoding"] == "utf8"
        jwk = json.loads(key_value["value"])
        assert set(jwk) == PUBLIC_EC_JWK_MEMBERS
        assert jwk["crv"] == "P-256" and jwk["kty"] == "EC"
        x = int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4)), "big")
        y = int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "=" * (-len(jwk["y"]) % 4)), "big")
        public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    assert isinstance(public_key.curve, ec.SECP256R1)
    return public_key, spki_der


def _check_signature_fixture(
    case: dict[str, Any], fixture: SignatureFixture, coverage_errors: list[str]
) -> bytes | None:
    values_by_name = {value["name"]: value for _, value in _case_named_values(case)}
    if fixture.high_s_name not in values_by_name:
        coverage_errors.append(f"{fixture.case_id}: missing value {fixture.high_s_name}")
        return None
    if fixture.high_s_name not in {item["field"] for item in case["substitutes"]}:
        coverage_errors.append(f"{fixture.case_id}: missing substitute {fixture.high_s_name}")
    if not any(
        mutation["field"] == fixture.high_s_name
        and mutation["operation"] == "substitute"
        and mutation["expected_code"] == "authentication_required"
        for mutation in case["mutations"]
    ):
        coverage_errors.append(f"{fixture.case_id}: missing rejection mutation {fixture.high_s_name}")

    positive = _decode_named_value(_named_value(case, fixture.positive_group, fixture.positive_name))
    high_s = _decode_named_value(values_by_name[fixture.high_s_name])
    assert len(positive) == len(high_s) == 64
    assert positive[:32] == high_s[:32]
    positive_r = int.from_bytes(positive[:32], "big")
    positive_s = int.from_bytes(positive[32:], "big")
    high_s_integer = int.from_bytes(high_s[32:], "big")
    assert 0 < positive_r < P256_ORDER
    assert 0 < positive_s <= P256_ORDER // 2 < high_s_integer < P256_ORDER
    assert high_s_integer == P256_ORDER - positive_s

    public_key, spki_der = _load_signature_public_key(case, fixture)
    message_value = _named_value(case, fixture.message_group, fixture.message_name)
    if message_value["encoding"] == "hex":
        message = bytes.fromhex(message_value["value"])
    else:
        assert message_value["encoding"] in {"ascii", "utf8"}
        message = message_value["value"].encode(message_value["encoding"])
    public_key.verify(encode_dss_signature(positive_r, positive_s), message, ec.ECDSA(hashes.SHA256()))
    public_key.verify(
        encode_dss_signature(positive_r, high_s_integer),
        message,
        ec.ECDSA(hashes.SHA256()),
    )
    return spki_der


def test_every_published_p256_signature_and_high_s_twin_verifies_and_high_s_is_rejected() -> None:
    vector_documents = (
        _load(VECTORS / "key-rotations.json"),
        _load(VECTORS / "managed-enrollment.json"),
        _load(VECTORS / "http-signatures.json"),
    )
    cases = {case["id"]: case for document in vector_documents for case in document["cases"]}
    fixtures = [*PUBLISHED_SIGNATURE_FIXTURES]
    fixtures.extend(
        SignatureFixture(
            case_id=case["id"],
            positive_group="intermediates",
            positive_name="signature.rem.p1363",
            high_s_name="signature.rem.high-s",
            key_group="inputs",
            key_name="jwk.rem",
            message_group="intermediates",
            message_name="signature-base.rem",
        )
        for case in _load(VECTORS / "http-signatures.json")["cases"]
    )
    coverage_errors: list[str] = []
    spki_der_values = [
        spki_der
        for fixture in fixtures
        if (spki_der := _check_signature_fixture(cases[fixture.case_id], fixture, coverage_errors)) is not None
    ]
    assert len(spki_der_values) == len(set(spki_der_values)) == 2
    assert coverage_errors == []


def test_snapshot_continuation_example_advances_and_obeys_terminal_page_rules() -> None:
    document = _load(SCHEMAS / "control-api.schema.json")
    schema = document["$defs"]["rosterSnapshotResponse"]
    cases = {case["id"]: case for case in _load(VECTORS / "http-signatures.json")["cases"]}
    continuation = cases["http.route.control.roster.snapshot.continuation"]
    request_target = _named_value(continuation, "inputs", "request.target")["value"]
    request_token = parse_qs(urlsplit(request_target).query, strict_parsing=True)["continuation_token"][0]
    token_bytes = base64.urlsafe_b64decode(request_token + "=" * (-len(request_token) % 4))
    assert len(token_bytes) == 73 and token_bytes[0] == 1
    requested_page_index = int.from_bytes(token_bytes[25:33], "big")
    requested_record_offset = int.from_bytes(token_bytes[33:41], "big")
    response = json.loads(_named_value(continuation, "expected", "response.body")["value"])

    assert response["page_index"] == requested_page_index
    is_terminal = requested_record_offset + len(response["records"]) >= response["total_count"]
    assert response["complete"] is is_terminal
    if is_terminal:
        assert "completeness_digest" in response and "continuation_token" not in response
    else:
        assert response["continuation_token"] != request_token
        assert "completeness_digest" not in response
    _assert_schema_accepts(response, schema, document)

    prefix_count = int(_named_value(continuation, "inputs", "snapshot.prefix-record-count")["value"])
    template_text = _named_value(continuation, "inputs", "snapshot.prefix-record-template")["value"]
    template = json.loads(template_text.replace("<derived reporter_id>", "PLACEHOLDER"))
    prefix_ids = sorted(
        base64.urlsafe_b64encode(index.to_bytes(32, "big")).rstrip(b"=").decode() for index in range(prefix_count)
    )
    records = [{**template, "reporter_id": reporter_id} for reporter_id in prefix_ids] + response["records"]
    assert len(records) == response["total_count"] == 101
    assert [record["reporter_id"] for record in records] == sorted(record["reporter_id"] for record in records)

    canonical_records = [json.dumps(record, sort_keys=True, separators=(",", ":")).encode() for record in records]
    record_stream = b"".join(len(record).to_bytes(4, "big") + record for record in canonical_records)
    records_sha256 = hashlib.sha256(record_stream).digest()
    assert (
        records_sha256.hex() == _named_value(continuation, "intermediates", "snapshot.records.stream.sha256")["value"]
    )

    purpose = b"rem-roster-snapshot-v1"

    def field(name: bytes, value: bytes) -> bytes:
        return len(name).to_bytes(2, "big") + name + len(value).to_bytes(4, "big") + value

    transcript = (
        b"REM-TRANSCRIPT\x01"
        + len(purpose).to_bytes(2, "big")
        + purpose
        + field(b"schema_version", b"\x01")
        + field(b"purpose", purpose)
        + field(b"snapshot_id", _decode_named_value({"encoding": "base64url", "value": response["snapshot_id"]}))
        + field(b"snapshot_generation", response["snapshot_generation"].to_bytes(8, "big"))
        + field(b"total_count", response["total_count"].to_bytes(8, "big"))
        + field(b"records_sha256", records_sha256)
    )
    assert transcript.hex() == _named_value(continuation, "intermediates", "snapshot.completeness-transcript")["value"]
    completeness_digest = hashlib.sha256(transcript).digest()
    assert (
        completeness_digest.hex()
        == _named_value(continuation, "intermediates", "snapshot.completeness-transcript.sha256")["value"]
    )
    assert base64.urlsafe_b64encode(completeness_digest).rstrip(b"=").decode() == response["completeness_digest"]

    initial = json.loads(
        _named_value(cases["http.route.control.roster.snapshot"], "expected", "response.body")["value"]
    )
    assert initial["complete"] is True
    assert "completeness_digest" in initial and "continuation_token" not in initial
    assert not _schema_accepts({**initial, "continuation_token": request_token}, schema, document)
    assert not _schema_accepts({**response, "complete": False}, schema, document)


def test_control_key_rotation_completion_declares_full_http_mutation_and_replay_coverage() -> None:
    case = next(case for case in _load(VECTORS / "key-rotations.json")["cases"] if case["id"] == "rotation.control-key")
    mutation_fields = {mutation["field"] for mutation in case["mutations"]}
    common_fields = {
        "request.method",
        "request.authority",
        "request.target.path",
        "request.target.empty-query",
        "headers.rem-purpose",
        "headers.rem-control-key-generation",
        "headers.rem-request-nonce",
        "headers.rem-request-expires",
        "headers.content-type",
        "headers.content-digest",
        "body",
        "request.replay.after-expiry",
        "request.target.cross-route",
        "headers.rem-purpose.cross-purpose",
    }
    per_signature_fields = {
        f"signature-params.{label}.{field}"
        for label in ("rem", "rem-proposed")
        for field in ("created", "expires", "nonce", "keyid", "alg", "tag", "components", "component-list")
    }
    per_signature_fields |= {
        f"signature.{label}.{form}" for label in ("rem", "rem-proposed") for form in ("p1363", "high-s")
    }
    per_signature_fields |= {"headers.signature.rem", "headers.signature.rem-proposed", "signature-input.labels"}
    assert common_fields | per_signature_fields <= mutation_fields

    mutations = {mutation["field"]: mutation for mutation in case["mutations"]}
    assert mutations["request.replay.after-expiry"]["operation"] == "duplicate"
    assert mutations["request.replay.after-expiry"]["expected_code"] == "authentication_required"
    route = next(
        route
        for route in _load(PROFILE / "routes.json")["routes"]
        if route["id"] == "control.control-key-rotation.complete"
    )
    replay_rule = route["replay_rule"].lower()
    assert "exact request-hash and nonce match" in replay_rule
    assert "unexpired" in replay_rule and "returns the recorded result" in replay_rule
    substitutes = {item["field"] for item in case["substitutes"]}
    assert {
        "request.target.cross-route",
        "headers.rem-purpose.cross-purpose",
        "signature.rem.high-s",
        "signature.rem-proposed.high-s",
    } <= substitutes
    assert mutations["request.target.cross-route"]["operation"] == "substitute"
    destination_route = next(
        route for route in _load(PROFILE / "routes.json")["routes"] if route["id"] == "control.mqtt-key.provision"
    )
    destination_schema_name = destination_route["request_schema"].split("#/$defs/", 1)[1]
    destination_document = _load(SCHEMAS / "control-api.schema.json")
    destination_schema = destination_document["$defs"][destination_schema_name]
    retained_body = json.loads(_named_value(case, "inputs", "body")["value"])
    assert not _schema_accepts(retained_body, destination_schema, destination_document)
    assert mutations["request.target.cross-route"]["expected_code"] == "invalid_request"
    assert mutations["headers.rem-purpose.cross-purpose"]["operation"] == "substitute"
    assert mutations["headers.rem-purpose.cross-purpose"]["expected_code"] == "authentication_required"


def test_completed_vector_task_requires_real_sas_words_or_an_explicit_approved_split() -> None:
    tasks = (ROOT / ".kiro" / "specs" / "native-computer-reporters" / "tasks.md").read_text(encoding="utf-8")
    placeholders = [
        path.name for path in sorted(VECTORS.glob("*.json")) if "sas-placeholder" in path.read_text(encoding="utf-8")
    ]
    if "- [x] 3.1 " in tasks and placeholders:
        readme = " ".join((PROFILE / "README.md").read_text(encoding="utf-8").split())
        wire_profile = " ".join((PROFILE / "wire-profile.md").read_text(encoding="utf-8").split())
        assert "normative SAS word tables (task 3.3)" in readme
        assert "task 3.3 pins and vendors both tables as normative data" in wire_profile
        assert "MUST NOT be shown to a user or accepted by an implementation" in wire_profile


def test_rfc3339_utc_schema_rejects_impossible_gregorian_dates() -> None:
    document = _load(SCHEMAS / "common.schema.json")
    schema = document["$defs"]["rfc3339Utc"]
    valid = {
        "2000-02-29T00:00:00Z",
        "2024-02-29T23:59:59Z",
        "2026-04-30T12:34:56Z",
    }
    invalid = {
        "0000-01-01T00:00:00Z",
        "1900-02-29T00:00:00Z",
        "2025-02-29T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-04-31T00:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-01-01T24:00:00Z",
        "2026-01-01T00:00:60Z",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00.0Z",
    }
    assert schema["format"] == "date-time"
    assert schema["x-rem-semantic-validation"] == "rem-rfc3339-utc-gregorian-v1"
    assert all(_schema_accepts(value, schema, document) for value in valid)
    assert not any(_schema_accepts(value, schema, document) for value in invalid)
