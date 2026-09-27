"""ES bulk 요청/응답 해석과 index template ↔ C-02 schema 필드 일치."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.indexer.contract import SECURITY_EVENT_SCHEMA, default_contracts_dir, verify_contract_snapshot
from pipeline.indexer.es import BulkAction, BulkRequestError, build_bulk_body, parse_bulk_response

TEMPLATE = Path(__file__).resolve().parents[2] / "es" / "security-events-v2-template.json"


def actions(*ids):
    return [BulkAction("security-events-v2-2026.09.27", i, {"event_id": i}) for i in ids]


def test_bulk_body_uses_index_action_with_event_id_as_doc_id():
    body = build_bulk_body(actions("a", "b")).decode()
    lines = body.splitlines()
    assert body.endswith("\n") and len(lines) == 4
    assert json.loads(lines[0]) == {"index": {"_index": "security-events-v2-2026.09.27", "_id": "a"}}
    assert json.loads(lines[1]) == {"event_id": "a"}


def test_item_results_are_read_per_item_even_when_http_ok():
    resp = {
        "errors": True,
        "items": [
            {"index": {"_id": "a", "status": 201}},
            {"index": {"_id": "b", "status": 400, "error": {"type": "index_closed_exception"}}},
            {"index": {"_id": "c", "status": 200}},
            {"index": {"_id": "d", "status": 429, "error": {"type": "es_rejected_execution_exception"}}},
        ],
    }
    results = parse_bulk_response(actions("a", "b", "c", "d"), resp)
    assert [r.ok for r in results] == [True, False, True, False]
    assert results[1].error_type == "index_closed_exception" and results[1].status == 400


def test_error_type_that_is_not_a_plain_identifier_is_not_propagated():
    resp = {"items": [{"index": {"_id": "a", "status": 400, "error": {"type": "Bearer eyJabc.def.ghi"}}}]}
    assert parse_bulk_response(actions("a"), resp)[0].error_type == "unknown"


@pytest.mark.parametrize(
    "resp",
    [
        {"items": [{"index": {"_id": "a", "status": 201}}]},  # 항목 수 불일치
        {"items": [{"index": {"_id": "x", "status": 201}}, {"index": {"_id": "b", "status": 201}}]},  # _id 불일치
        {"items": [{"create": {"_id": "a", "status": 201}}, {"index": {"_id": "b", "status": 201}}]},  # 다른 action
        {"items": [{"index": {"_id": "a"}}, {"index": {"_id": "b", "status": 201}}]},  # status 없음
        {"errors": False},
        [],
    ],
)
def test_untrustworthy_bulk_response_fails_whole_request(resp):
    with pytest.raises(BulkRequestError):
        parse_bulk_response(actions("a", "b"), resp)


def _schema_fields(schema: dict, node: dict) -> dict:
    """schema 노드에서 object 속성 트리를 뽑는다($ref·nullable anyOf 해석)."""
    if "$ref" in node:
        node = schema["$defs"][node["$ref"].split("/")[-1]]
    if "anyOf" in node:
        objects = [n for n in node["anyOf"] if n.get("type") != "null"]
        assert len(objects) == 1
        return _schema_fields(schema, objects[0])
    if node.get("type") == "object" and "properties" in node:
        return {k: _schema_fields(schema, v) for k, v in node["properties"].items()}
    return {}


def _mapping_fields(props: dict) -> dict:
    return {k: _mapping_fields(v.get("properties", {})) for k, v in props.items()}


def test_template_is_strict_and_matches_contract_fields_exactly():
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    schema = json.loads((default_contracts_dir() / SECURITY_EVENT_SCHEMA).read_text(encoding="utf-8"))
    mappings = template["template"]["mappings"]
    assert template["index_patterns"] == ["security-events-v2-*"]
    assert mappings["dynamic"] == "strict"
    assert _mapping_fields(mappings["properties"]) == _schema_fields(schema, schema)


def test_template_types_for_ids_enums_and_timestamps():
    props = json.loads(TEMPLATE.read_text(encoding="utf-8"))["template"]["mappings"]["properties"]
    for key in ("event_id", "request_id", "schema_version", "producer", "event_type", "outcome", "environment"):
        assert props[key]["type"] == "keyword", key
    assert props["occurred_at"]["type"] == "date"
    assert props["http"]["properties"]["status_code"]["type"] in ("short", "integer")


def test_contract_snapshot_matches_manifest():
    info = verify_contract_snapshot(default_contracts_dir())
    assert info.revision.startswith("sha256:")
