#!/usr/bin/env python3
"""C-02 공통 계약 검증기.

세 단계로 검사한다.

1. parse    : 엄격한 JSON 파싱(중복 key, NaN/Infinity 거부).
2. schema   : draft 2020-12 JSON Schema + format(uuid/date-time) 검사.
3. semantic : JSON Schema로 표현할 수 없는 의미 규칙.
   - 단일 문서: detection_id 결정 규칙, score↔flag 일치, window/cutoff 순서,
     command 유효 기간·evidence_ref 일치, 결과 applied_at 순서.
   - 여러 문서(scenario): event_id 충돌, request 중복 집계, 미래 시각,
     late/out-of-order 분류, UTC index 날짜, command 결과의 중복·만료·stale 집행.

출력에는 규칙 ID와 위치(JSON pointer)만 쓴다. jsonschema 메시지는 입력 값을
그대로 포함하므로 원문 비밀이 섞인 문서를 검사할 때 로그로 새지 않게 출력하지 않는다.

사용법:
  validate.py FILE...          schema_version으로 계약을 판별해 schema+의미 검사
  validate.py --fixtures       모든 fixtures/index.json의 기대 결과와 대조
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

CONTRACTS_DIR = Path(__file__).resolve().parents[1]

# schema_version → schema 파일(contracts/ 기준 상대 경로)
SCHEMA_FILES: Dict[str, str] = {
    "security-event/2.0": "security-event/v2/schema.json",
    "anomaly-detection/1.0": "anomaly-detection/v1/schema.json",
    "response-command/1.0": "response-command/v1/schema.json",
    "response-result/1.0": "response-command/v1/result.schema.json",
}

CONTRACT_DIRS: Tuple[str, ...] = (
    "security-event/v2",
    "anomaly-detection/v1",
    "response-command/v1",
)

DETECTION_ID_DOMAIN = "zetty:anomaly-detection:id:v1"
INDEX_PREFIX_SECURITY_EVENT = "zetty-security-events-v2-"

REQUIRED_FORMATS = ("uuid", "date-time")


class Violation(NamedTuple):
    layer: str  # parse | schema | semantic
    rule: str  # allOf title, JSON pointer("#/..."), 또는 semantic 규칙 ID
    location: str  # JSON pointer 또는 scenario 내 위치
    detail: str  # 값이 들어가지 않는 짧은 설명


class ContractParseError(ValueError):
    pass


# ---------------------------------------------------------------- parse


def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ContractParseError("DUPLICATE_JSON_KEY")
        seen[key] = value
    return seen


def _reject_constant(_name: str) -> Any:
    raise ContractParseError("NON_FINITE_NUMBER")


def loads_strict(text: str) -> Any:
    """중복 key·NaN·Infinity를 거부하는 JSON 파서. Java(Jackson) 쪽과 해석이 갈리는 입력을 막는다."""
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)


def load_strict(path: Path) -> Any:
    return loads_strict(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- time

_UTC_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?Z$",
    re.ASCII,
)


def parse_utc(value: str) -> datetime:
    """schema의 utc_timestamp 표기만 받는다(Python 3.9 fromisoformat은 'Z'와 1~6자리 소수를 처리하지 못함)."""
    match = _UTC_RE.match(value)
    if not match:
        raise ValueError("not a UTC RFC3339 timestamp")
    year, month, day, hour, minute, second, frac = match.groups()
    micro = int((frac or "0").ljust(6, "0"))
    return datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo=timezone.utc
    )


# ---------------------------------------------------------------- schema


@functools.lru_cache(maxsize=None)
def load_schema(rel_path: str) -> Dict[str, Any]:
    return load_strict(CONTRACTS_DIR / rel_path)


@functools.lru_cache(maxsize=None)
def get_validator(rel_path: str) -> Draft202012Validator:
    missing = [fmt for fmt in REQUIRED_FORMATS if fmt not in FormatChecker.checkers]
    if missing:
        # format 검사가 조용히 꺼진 채 통과하는 것을 막는다.
        raise RuntimeError("format checker unavailable: " + ", ".join(missing))
    schema = load_schema(rel_path)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def json_pointer(path: Iterable[Any]) -> str:
    parts = [str(p).replace("~", "~0").replace("/", "~1") for p in path]
    return "#" + "".join("/" + p for p in parts)


def _leaf_errors(error: ValidationError) -> List[ValidationError]:
    """anyOf/oneOf 오류에서 '의도한 branch'의 하위 오류를 찾는다.

    branch 자체가 type 불일치로 탈락했으면(예: object 값에 대한 null branch) 제외한다.
    """
    if not error.context:
        return [error]
    branches: Dict[Any, List[ValidationError]] = {}
    for sub in error.context:
        key = sub.relative_schema_path[0] if sub.relative_schema_path else None
        branches.setdefault(key, []).append(sub)
    candidates: List[ValidationError] = []
    for subs in branches.values():
        if any(s.validator == "type" and not s.relative_path for s in subs):
            continue
        candidates.extend(subs)
    if not candidates:
        return [error]
    leaves: List[ValidationError] = []
    for sub in candidates:
        leaves.extend(_leaf_errors(sub))
    return leaves


def error_rules(error: ValidationError, schema: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """(rule, location, keyword) 목록. top-level allOf 규칙은 title, 그 밖은 위반 위치 pointer."""
    path = list(error.schema_path)
    if len(path) >= 2 and path[0] == "allOf" and isinstance(path[1], int):
        title = schema["allOf"][path[1]].get("title", "allOf/%d" % path[1])
        return [(title, json_pointer(leaf.absolute_path), str(leaf.validator)) for leaf in _leaf_errors(error)]
    return [
        (json_pointer(leaf.absolute_path), json_pointer(leaf.absolute_path), str(leaf.validator))
        for leaf in _leaf_errors(error)
    ]


def schema_violations(doc: Any, schema_rel_path: Optional[str] = None) -> List[Violation]:
    if schema_rel_path is None:
        version = doc.get("schema_version") if isinstance(doc, dict) else None
        schema_rel_path = SCHEMA_FILES.get(version) if isinstance(version, str) else None
        if schema_rel_path is None:
            return [Violation("schema", "#/schema_version", "#/schema_version", "unknown schema_version")]
    validator = get_validator(schema_rel_path)
    schema = load_schema(schema_rel_path)
    found: Dict[Tuple[str, str, str], Violation] = {}
    for error in validator.iter_errors(doc):
        for rule, location, keyword in error_rules(error, schema):
            found.setdefault((rule, location, keyword), Violation("schema", rule, location, "keyword=" + keyword))
    return sorted(found.values())


# ---------------------------------------------------------------- semantic: single document


def derive_detection_id(doc: Dict[str, Any]) -> str:
    """detection_id = sha256(도메인 + 결정 필드를 '\\n'으로 연결한 UTF-8). null은 빈 문자열."""
    window = doc.get("window") or {}
    origin = doc["data_origin"]
    fields = [
        DETECTION_ID_DOMAIN,
        doc["environment"],
        origin["kind"],
        origin["dataset_id"] or "",
        doc["detector_id"],
        doc["target_type"],
        doc["target_key"]["key_version"],
        doc["target_key"]["key"],
        window.get("start") or "",
        window.get("end") or "",
        doc["event_ref"] or "",
        doc["feature_version"],
        doc["model_version"],
        doc["threshold_version"],
        doc["input_hash"],
    ]
    return hashlib.sha256("\n".join(fields).encode("utf-8")).hexdigest()


def _semantic_anomaly_detection(doc: Dict[str, Any]) -> List[Violation]:
    out: List[Violation] = []
    if doc["detection_id"] != derive_detection_id(doc):
        out.append(Violation("semantic", "AD_DETECTION_ID_MISMATCH", "#/detection_id", "not derived from key fields"))
    cutoff = parse_utc(doc["cutoff"])
    window = doc["window"]
    if window is not None:
        start, end = parse_utc(window["start"]), parse_utc(window["end"])
        if not (start < end <= cutoff):
            out.append(Violation("semantic", "AD_WINDOW_ORDER", "#/window", "requires start < end <= cutoff"))
    if parse_utc(doc["detected_at"]) < cutoff:
        out.append(Violation("semantic", "AD_DETECTED_BEFORE_CUTOFF", "#/detected_at", "detected_at < cutoff"))
    if doc["status"] == "EVALUATED":
        score, threshold = doc["anomaly_score"], doc["threshold"]
        expected = score > threshold if doc["threshold_operator"] == "GT" else score >= threshold
        if doc["is_anomaly"] is not expected:
            out.append(Violation("semantic", "AD_FLAG_INCONSISTENT", "#/is_anomaly", "flag != score op threshold"))
    return out


def _semantic_response_command(doc: Dict[str, Any]) -> List[Violation]:
    out: List[Violation] = []
    if parse_utc(doc["expires_at"]) <= parse_utc(doc["requested_at"]):
        out.append(Violation("semantic", "RC_EXPIRES_NOT_AFTER_REQUESTED", "#/expires_at", "expires_at <= requested_at"))
    kind, _, ref_id = doc["evidence_ref"].partition(":")
    expected = doc["detection_id"] if kind == "anomaly-detection" else doc["incident_id"]
    if ref_id != expected:
        out.append(Violation("semantic", "RC_EVIDENCE_REF_MISMATCH", "#/evidence_ref", "evidence_ref id != basis id"))
    return out


def _semantic_response_result(doc: Dict[str, Any]) -> List[Violation]:
    out: List[Violation] = []
    if doc["applied_at"] is not None and parse_utc(doc["applied_at"]) > parse_utc(doc["recorded_at"]):
        out.append(Violation("semantic", "RR_APPLIED_AFTER_RECORDED", "#/applied_at", "applied_at > recorded_at"))
    return out


_SINGLE_SEMANTIC = {
    "security-event/2.0": lambda doc: [],
    "anomaly-detection/1.0": _semantic_anomaly_detection,
    "response-command/1.0": _semantic_response_command,
    "response-result/1.0": _semantic_response_result,
}


def semantic_violations(doc: Dict[str, Any]) -> List[Violation]:
    """schema를 통과한 문서에만 호출한다."""
    return _SINGLE_SEMANTIC[doc["schema_version"]](doc)


def validate_document(doc: Any, schema_rel_path: Optional[str] = None) -> List[Violation]:
    """schema 위반이 있으면 schema 위반만, 없으면 단일 문서 의미 위반을 반환한다."""
    violations = schema_violations(doc, schema_rel_path)
    if violations:
        return violations
    return semantic_violations(doc)


# ---------------------------------------------------------------- semantic: scenarios


def security_event_index_name(event: Dict[str, Any]) -> str:
    """raw index 날짜는 UTC occurred_at 기준(ingestion/replay 날짜가 아님)."""
    return INDEX_PREFIX_SECURITY_EVENT + parse_utc(event["occurred_at"]).strftime("%Y.%m.%d")


def check_event_batch(records: List[Dict[str, Any]], params: Dict[str, Any]) -> Tuple[List[Violation], Dict[str, Any]]:
    """collector 관점의 여러 SecurityEvent 의미 검사.

    records: [{"first_received_at": UTC, "event": SecurityEvent}] (수신 순서)
    params : window_seconds, allowed_lateness_seconds, max_future_skew_seconds
    """
    window = timedelta(seconds=int(params["window_seconds"]))
    lateness = timedelta(seconds=int(params["allowed_lateness_seconds"]))
    max_skew = timedelta(seconds=int(params["max_future_skew_seconds"]))
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    violations: List[Violation] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    first_received: Dict[str, datetime] = {}
    order: List[str] = []

    for i, record in enumerate(records):
        event = record["event"]
        event_id = event["event_id"]
        received = parse_utc(record["first_received_at"])
        where = "#/records/%d" % i
        if parse_utc(event["occurred_at"]) > received + max_skew:
            violations.append(Violation("semantic", "SE_FUTURE_OCCURRED_AT", where, "occurred_at beyond allowed skew"))
        if event_id in by_id:
            if by_id[event_id] != event:
                # 서로 다른 관측이 같은 event_id로 덮어써지면 안 된다.
                violations.append(Violation("semantic", "SE_EVENT_ID_CONFLICT", where, "same event_id, different content"))
            # 동일 내용의 retry/replay: 최초 수신 시각을 유지한다.
            first_received[event_id] = min(first_received[event_id], received)
            continue
        by_id[event_id] = event
        first_received[event_id] = received
        order.append(event_id)

    # 요청 한 번은 (request_id, producer, event_type)마다 관측 하나다.
    per_request: Dict[Tuple[str, str, str], List[str]] = {}
    for event_id in order:
        event = by_id[event_id]
        if event["request_id"] and event["event_type"] in ("ACCESS_DECISION", "BUSINESS_RESULT", "EDGE_COMPLETED"):
            per_request.setdefault((event["request_id"], event["producer"], event["event_type"]), []).append(event_id)
    for key, ids in sorted(per_request.items()):
        if len(ids) > 1:
            violations.append(Violation("semantic", "SE_REQUEST_DOUBLE_COUNT", "request_id=" + key[0], "multiple observations of one request"))

    access = {
        by_id[i]["request_id"]: i
        for i in order
        if by_id[i]["event_type"] == "ACCESS_DECISION" and by_id[i]["producer"] == "api"
    }
    edge = {by_id[i]["request_id"]: i for i in order if by_id[i]["event_type"] == "EDGE_COMPLETED"}
    joined = [
        {"request_id": rid, "access_event_id": access[rid], "edge_event_id": edge[rid]}
        for rid in sorted(set(access) & set(edge))
    ]

    late: List[str] = []
    for event_id in order:
        occurred = parse_utc(by_id[event_id]["occurred_at"])
        window_start = epoch + ((occurred - epoch) // window) * window
        cutoff = window_start + window + lateness
        if first_received[event_id] >= cutoff:
            late.append(event_id)

    summary = {
        "unique_event_ids": order,
        "first_received_at": {i: _fmt(first_received[i]) for i in order},
        "behavior_count": len(access),
        "joined_requests": joined,
        "event_time_order": sorted(order, key=lambda i: (parse_utc(by_id[i]["occurred_at"]), i)),
        "late_event_ids": late,
        "index_names": {i: security_event_index_name(by_id[i]) for i in order},
    }
    return violations, summary


def check_detection_batch(detections: List[Dict[str, Any]]) -> Tuple[List[Violation], Dict[str, Any]]:
    violations: List[Violation] = []
    keys: Dict[str, str] = {}
    for i, doc in enumerate(detections):
        derived = derive_detection_id(doc)
        if doc["detection_id"] != derived:
            violations.append(Violation("semantic", "AD_DETECTION_ID_MISMATCH", "#/detections/%d" % i, "not derived"))
        keys.setdefault(doc["detection_id"], derived)
    return violations, {"distinct_detection_ids": len(keys), "detection_ids": [d["detection_id"] for d in detections]}


def check_response_lifecycle(command: Dict[str, Any], results: List[Dict[str, Any]]) -> Tuple[List[Violation], Dict[str, Any]]:
    """명령 하나와 그 결과들(기록 순서)의 의미 검사."""
    violations: List[Violation] = []
    expires = parse_utc(command["expires_at"])
    applied_at: Optional[str] = None
    applied_count = 0
    for i, result in enumerate(results):
        where = "#/results/%d" % i
        if result["command_id"] != command["command_id"]:
            violations.append(Violation("semantic", "RR_COMMAND_ID_MISMATCH", where, "result for another command"))
        if result["mode"] != command["mode"]:
            violations.append(Violation("semantic", "RR_MODE_MISMATCH", where, "result mode != command mode"))
        status = result["status"]
        if status in ("APPLIED", "DRY_RUN") and parse_utc(result["recorded_at"]) >= expires:
            violations.append(Violation("semantic", "RR_EXECUTED_AFTER_EXPIRY", where, "expired command executed"))
        if status == "APPLIED":
            applied_count += 1
            if applied_count > 1:
                violations.append(Violation("semantic", "RR_DUPLICATE_APPLY", where, "command_id applied twice"))
            if (
                command["expected_state_version"] is not None
                and result["observed_state_version"] != command["expected_state_version"]
            ):
                violations.append(Violation("semantic", "RR_STALE_APPLIED", where, "state version changed but applied"))
            if applied_at is None:
                applied_at = result["applied_at"]
        if status == "ALREADY_APPLIED" and (applied_at is None or result["applied_at"] != applied_at):
            violations.append(Violation("semantic", "RR_ALREADY_APPLIED_WITHOUT_PRIOR", where, "no prior APPLIED with same applied_at"))
    summary = {
        "applied_count": applied_count,
        "statuses": [r["status"] for r in results],
    }
    return violations, summary


def _fmt(value: datetime) -> str:
    text = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        text += (".%06d" % value.microsecond).rstrip("0")
    return text + "Z"


def run_scenario(scenario: Dict[str, Any]) -> Tuple[List[Violation], Dict[str, Any]]:
    """scenario 안의 각 문서는 schema·단일 의미 검사를 먼저 통과해야 한다."""
    kind = scenario["scenario"]
    docs: List[Tuple[str, Dict[str, Any]]] = []
    if kind == "security-event-batch":
        docs = [("#/records/%d/event" % i, r["event"]) for i, r in enumerate(scenario["records"])]
    elif kind == "anomaly-detection-batch":
        docs = [("#/detections/%d" % i, d) for i, d in enumerate(scenario["detections"])]
    elif kind == "response-lifecycle":
        docs = [("#/command", scenario["command"])] + [("#/results/%d" % i, r) for i, r in enumerate(scenario["results"])]
    else:
        raise ValueError("unknown scenario kind")

    doc_violations: List[Violation] = []
    for where, doc in docs:
        for v in validate_document(doc):
            doc_violations.append(v._replace(location=where + v.location[1:]))
    if doc_violations:
        return doc_violations, {}

    if kind == "security-event-batch":
        return check_event_batch(scenario["records"], scenario["params"])
    if kind == "anomaly-detection-batch":
        return check_detection_batch(scenario["detections"])
    return check_response_lifecycle(scenario["command"], scenario["results"])


# ---------------------------------------------------------------- fixtures index


def iter_index_entries() -> Iterable[Tuple[Path, Dict[str, Any]]]:
    for contract in CONTRACT_DIRS:
        base = CONTRACTS_DIR / contract / "fixtures"
        index = load_strict(base / "index.json")
        for entry in index["fixtures"]:
            yield base, entry


def check_fixture(base: Path, entry: Dict[str, Any]) -> List[str]:
    """index.json 항목 하나가 기대대로 동작하는지 확인하고 불일치 설명 목록을 반환한다."""
    path = base / entry["path"]
    expect, layer, rule = entry["expect"], entry.get("layer"), entry.get("rule")
    problems: List[str] = []
    try:
        doc = load_strict(path)
    except ContractParseError as exc:
        if not (expect == "invalid" and layer == "parse" and rule == str(exc)):
            problems.append("parse error %s" % exc)
        return problems
    if expect == "invalid" and layer == "parse":
        return ["expected parse error %s" % rule]

    if entry.get("kind") == "scenario":
        violations, summary = run_scenario(doc)
        if expect == "valid":
            if violations:
                problems.append("unexpected %s" % sorted({v.rule for v in violations}))
            for key, value in doc.get("expect", {}).items():
                if summary.get(key) != value:
                    problems.append("expect.%s mismatch" % key)
        else:
            rules = sorted({v.rule for v in violations})
            if rules != [rule] or any(v.layer != layer for v in violations):
                problems.append("expected only %s/%s, got %s" % (layer, rule, rules))
        return problems

    schema_rel = (base.parent.relative_to(CONTRACTS_DIR) / entry["schema"]).as_posix()
    s_violations = schema_violations(doc, schema_rel)
    if expect == "valid":
        if s_violations:
            problems.append("schema %s" % sorted({v.rule for v in s_violations}))
        else:
            sem = semantic_violations(doc)
            if sem:
                problems.append("semantic %s" % sorted({v.rule for v in sem}))
        return problems
    if layer == "schema":
        rules = sorted({v.rule for v in s_violations})
        if rules != [rule]:
            problems.append("expected only schema rule %s, got %s" % (rule, rules))
        return problems
    # layer == semantic: schema는 통과하고 의미 규칙 하나만 위반해야 한다.
    if s_violations:
        problems.append("expected schema pass, got %s" % sorted({v.rule for v in s_violations}))
        return problems
    rules = sorted({v.rule for v in semantic_violations(doc)})
    if rules != [rule]:
        problems.append("expected only semantic rule %s, got %s" % (rule, rules))
    return problems


# ---------------------------------------------------------------- CLI


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="C-02 계약 검증기(schema + 의미 검사)")
    parser.add_argument("files", nargs="*", type=Path, help="검사할 JSON 문서")
    parser.add_argument("--fixtures", action="store_true", help="fixtures/index.json 기대 결과 대조")
    args = parser.parse_args(argv)

    failed = False
    if args.fixtures:
        count = 0
        for base, entry in iter_index_entries():
            count += 1
            problems = check_fixture(base, entry)
            label = str((base / entry["path"]).relative_to(CONTRACTS_DIR))
            if problems:
                failed = True
                print("FAIL %s: %s" % (label, "; ".join(problems)))
        print("fixtures checked: %d, %s" % (count, "FAILED" if failed else "OK"))

    for path in args.files:
        try:
            doc = load_strict(path)
        except (ContractParseError, json.JSONDecodeError) as exc:
            failed = True
            print("INVALID %s parse %s" % (path, type(exc).__name__ if isinstance(exc, json.JSONDecodeError) else exc))
            continue
        if isinstance(doc, dict) and "scenario" in doc:
            violations, _ = run_scenario(doc)
        else:
            violations = validate_document(doc)
        if violations:
            failed = True
            for v in violations:
                print("INVALID %s %s %s %s (%s)" % (path, v.layer, v.rule, v.location, v.detail))
        else:
            print("OK %s" % path)

    if not args.fixtures and not args.files:
        parser.print_usage()
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
