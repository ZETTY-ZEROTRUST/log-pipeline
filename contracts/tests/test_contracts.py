"""C-02 공통 계약 테스트.

실행: contracts/.venv/bin/python -m unittest discover -s contracts/tests -v
"""

from __future__ import annotations

import base64
import copy
import importlib
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

CONTRACTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTRACTS_DIR / "tools"))

import validate  # noqa: E402

manifest_tool = importlib.import_module("hash")

from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402

# contracts.md §7 C-02 완료 조건의 예제 목록
SECTION7_EXAMPLES = {
    "정상 Auth",
    "정상 READ",
    "정상 WRITE",
    "authn 실패",
    "미발급 digest",
    "authz 거부",
    "rollback",
    "edge 완료",
    "duplicate event",
    "API/edge join",
    "out-of-order/late",
    "missing bytes",
    "UNKNOWN network",
    "model unavailable",
    "response dry-run",
    "response retry",
    "response stale",
}


def _b64url(obj: Dict[str, Any]) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# 실제 자격 증명이 아닌 alg=none 가짜 토큰(테스트 시 생성).
FAKE_JWT = "%s.%s.%s" % (_b64url({"alg": "none"}), _b64url({"sub": "probe"}), "c2ln")
SECRET_PROBES = (FAKE_JWT, "Bearer " + FAKE_JWT, "Bearer opaque-token")


def index_entries() -> List[Tuple[Path, Dict[str, Any]]]:
    return list(validate.iter_index_entries())


def schema_rel(base: Path, entry: Dict[str, Any]) -> str:
    return (base.parent.relative_to(CONTRACTS_DIR) / entry["schema"]).as_posix()


def string_paths(node: Any, path: Tuple[Any, ...] = ()) -> Iterator[Tuple[Any, ...]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from string_paths(value, path + (key,))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from string_paths(value, path + (i,))
    elif isinstance(node, str):
        yield path


def set_path(doc: Any, path: Tuple[Any, ...], value: Any) -> Any:
    out = copy.deepcopy(doc)
    cur = out
    for key in path[:-1]:
        cur = cur[key]
    cur[path[-1]] = value
    return out


class EnvironmentTest(unittest.TestCase):
    def test_format_checkers_are_active(self) -> None:
        # format 검사가 조용히 꺼지면 달력상 잘못된 날짜가 통과한다.
        for fmt in validate.REQUIRED_FORMATS:
            self.assertIn(fmt, FormatChecker.checkers)

    def test_schemas_are_valid_draft_2020_12(self) -> None:
        for rel in validate.SCHEMA_FILES.values():
            with self.subTest(schema=rel):
                schema = validate.load_schema(rel)
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                Draft202012Validator.check_schema(schema)

    def test_every_object_schema_closes_additional_properties(self) -> None:
        # if/then/else/allOf/oneOf/not 아래는 규칙 조각(제약)이지 object 정의가 아니다.
        rule_keywords = {"allOf", "oneOf", "if", "then", "else", "not"}

        def walk(node: Any, where: str) -> Iterator[str]:
            if isinstance(node, dict):
                if "properties" in node and node.get("type") == "object":
                    if node.get("additionalProperties") is not False:
                        yield where
                for key, value in node.items():
                    if key in rule_keywords:
                        continue
                    yield from walk(value, where + "/" + str(key))
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    yield from walk(value, where + "/" + str(i))

        for rel in validate.SCHEMA_FILES.values():
            with self.subTest(schema=rel):
                self.assertEqual(list(walk(validate.load_schema(rel), "#")), [])


class FixtureIndexTest(unittest.TestCase):
    def test_index_lists_every_fixture_file_once(self) -> None:
        for contract in validate.CONTRACT_DIRS:
            base = CONTRACTS_DIR / contract / "fixtures"
            on_disk = sorted(
                p.relative_to(base).as_posix() for p in base.rglob("*.json") if p.name != "index.json"
            )
            listed = [e["path"] for e in validate.load_strict(base / "index.json")["fixtures"]]
            with self.subTest(contract=contract):
                self.assertEqual(len(listed), len(set(listed)), "duplicate index entry")
                self.assertEqual(sorted(listed), on_disk)

    def test_directory_matches_expectation(self) -> None:
        for base, entry in index_entries():
            with self.subTest(fixture=entry["path"]):
                top = entry["path"].split("/")[0]
                if top == "scenarios":
                    self.assertEqual(entry.get("kind"), "scenario")
                else:
                    self.assertEqual(top, entry["expect"])
                if entry["expect"] == "invalid":
                    self.assertIn(entry.get("layer"), ("parse", "schema", "semantic"))
                    self.assertTrue(entry.get("rule"))

    def test_section7_examples_are_covered(self) -> None:
        covered = set()
        for _, entry in index_entries():
            covered.update(entry.get("covers", []))
        self.assertEqual(SECTION7_EXAMPLES - covered, set())


class ValidFixtureTest(unittest.TestCase):
    def test_valid_fixtures_pass_schema_and_semantic(self) -> None:
        count = 0
        for base, entry in index_entries():
            if entry["expect"] != "valid" or entry.get("kind") == "scenario":
                continue
            count += 1
            with self.subTest(fixture=entry["path"]):
                doc = validate.load_strict(base / entry["path"])
                self.assertEqual(validate.schema_violations(doc, schema_rel(base, entry)), [])
                self.assertEqual(validate.semantic_violations(doc), [])
                # 자동 판별(schema_version) 경로도 같은 schema를 고른다.
                self.assertEqual(validate.SCHEMA_FILES[doc["schema_version"]], schema_rel(base, entry))
        self.assertGreater(count, 0)

    def test_security_event_valid_fixtures_form_a_consistent_batch(self) -> None:
        """valid fixture 전체를 하나의 batch로 보아도 event_id 충돌·중복 집계가 없어야 한다."""
        base = CONTRACTS_DIR / "security-event/v2/fixtures"
        records = []
        for path in sorted((base / "valid").glob("*.json")):
            event = validate.load_strict(path)
            records.append({"first_received_at": event["occurred_at"], "event": event})
        params = {"window_seconds": 300, "allowed_lateness_seconds": 60, "max_future_skew_seconds": 30}
        violations, summary = validate.check_event_batch(records, params)
        self.assertEqual(violations, [])
        self.assertEqual(len(summary["unique_event_ids"]), len(records))
        self.assertEqual(summary["late_event_ids"], [])


class InvalidFixtureTest(unittest.TestCase):
    def test_each_invalid_fixture_violates_exactly_its_named_rule(self) -> None:
        count = 0
        for base, entry in index_entries():
            if entry["expect"] != "invalid" or entry.get("kind") == "scenario":
                continue
            count += 1
            with self.subTest(fixture=entry["path"], rule=entry["rule"]):
                path = base / entry["path"]
                if entry["layer"] == "parse":
                    with self.assertRaises(validate.ContractParseError) as ctx:
                        validate.load_strict(path)
                    self.assertEqual(str(ctx.exception), entry["rule"])
                    # 일반 json 파서는 조용히 마지막 값을 택한다 → strict 파서가 필요한 이유.
                    json.loads(path.read_text(encoding="utf-8"))
                    continue
                doc = validate.load_strict(path)
                schema_errors = validate.schema_violations(doc, schema_rel(base, entry))
                if entry["layer"] == "schema":
                    self.assertEqual(sorted({v.rule for v in schema_errors}), [entry["rule"]])
                else:
                    self.assertEqual(schema_errors, [], "semantic fixture must pass schema")
                    self.assertEqual(sorted({v.rule for v in validate.semantic_violations(doc)}), [entry["rule"]])
        self.assertGreater(count, 0)

    def test_file_name_is_unique_per_rule_intent(self) -> None:
        # 규칙 이름이 파일 이름에 드러나야 한다는 요구를 기계적으로 보조: 같은 파일명 중복 금지.
        names = [Path(e["path"]).name for _, e in index_entries() if e["expect"] == "invalid"]
        self.assertEqual(len(names), len(set(names)))


class ScenarioTest(unittest.TestCase):
    def test_scenarios_match_expectations(self) -> None:
        count = 0
        for base, entry in index_entries():
            if entry.get("kind") != "scenario":
                continue
            count += 1
            with self.subTest(scenario=entry["path"]):
                scenario = validate.load_strict(base / entry["path"])
                violations, summary = validate.run_scenario(scenario)
                if entry["expect"] == "valid":
                    self.assertEqual(violations, [])
                    self.assertTrue(scenario.get("expect"), "valid scenario needs expect block")
                    for key, value in scenario["expect"].items():
                        self.assertEqual(summary[key], value, key)
                else:
                    self.assertEqual(sorted({v.rule for v in violations}), [entry["rule"]])
                    self.assertTrue(all(v.layer == "semantic" for v in violations))
        self.assertGreater(count, 0)

    def test_duplicate_retry_keeps_first_received_at_regardless_of_order(self) -> None:
        scenario = validate.load_strict(CONTRACTS_DIR / "security-event/v2/fixtures/scenarios/duplicate_event_retry.json")
        reversed_records = list(reversed(scenario["records"]))
        _, summary = validate.check_event_batch(reversed_records, scenario["params"])
        self.assertEqual(summary["first_received_at"], scenario["expect"]["first_received_at"])

    def test_late_classification_is_independent_of_arrival_order(self) -> None:
        scenario = validate.load_strict(CONTRACTS_DIR / "security-event/v2/fixtures/scenarios/out_of_order_late.json")
        _, forward = validate.check_event_batch(scenario["records"], scenario["params"])
        _, backward = validate.check_event_batch(list(reversed(scenario["records"])), scenario["params"])
        self.assertEqual(sorted(forward["late_event_ids"]), sorted(backward["late_event_ids"]))
        self.assertEqual(forward["event_time_order"], backward["event_time_order"])
        self.assertEqual(forward["index_names"], backward["index_names"])


class SecretGuardTest(unittest.TestCase):
    def test_no_string_field_accepts_raw_jwt_or_bearer(self) -> None:
        """valid fixture의 모든 문자열 위치에 원문 JWT/Bearer 값을 넣으면 schema가 거부해야 한다."""
        checked = 0
        for base, entry in index_entries():
            if entry["expect"] != "valid" or entry.get("kind") == "scenario":
                continue
            doc = validate.load_strict(base / entry["path"])
            rel = schema_rel(base, entry)
            for path in string_paths(doc):
                for probe in SECRET_PROBES:
                    checked += 1
                    with self.subTest(fixture=entry["path"], field="/".join(map(str, path)), probe=probe[:6]):
                        self.assertNotEqual(validate.schema_violations(set_path(doc, path, probe), rel), [])
        self.assertGreater(checked, 0)

    def test_output_does_not_echo_values(self) -> None:
        base = CONTRACTS_DIR / "security-event/v2/fixtures"
        doc = validate.load_strict(base / "invalid/raw_jwt_in_field.json")
        rendered = repr(validate.schema_violations(doc, "security-event/v2/schema.json"))
        self.assertNotIn("eyJ", rendered)


class StrictParseTest(unittest.TestCase):
    def test_duplicate_key_rejected(self) -> None:
        with self.assertRaises(validate.ContractParseError):
            validate.loads_strict('{"a": 1, "a": 2}')

    def test_non_finite_numbers_rejected(self) -> None:
        for text in ('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'):
            with self.subTest(text=text), self.assertRaises(validate.ContractParseError):
                validate.loads_strict(text)


class DetectionIdTest(unittest.TestCase):
    def _valid_detections(self) -> Dict[str, Dict[str, Any]]:
        base = CONTRACTS_DIR / "anomaly-detection/v1/fixtures/valid"
        return {p.stem: validate.load_strict(p) for p in sorted(base.glob("*.json"))}

    def test_detection_id_is_derived_from_key_fields(self) -> None:
        for name, doc in self._valid_detections().items():
            with self.subTest(fixture=name):
                self.assertEqual(doc["detection_id"], validate.derive_detection_id(doc))

    def test_status_and_detected_at_do_not_change_id(self) -> None:
        doc = self._valid_detections()["evaluated_http_normal"]
        retry = dict(doc, status="MODEL_UNAVAILABLE", detected_at="2026-09-27T00:00:00Z")
        self.assertEqual(validate.derive_detection_id(retry), doc["detection_id"])

    def test_each_key_field_changes_id(self) -> None:
        doc = self._valid_detections()["evaluated_http_normal"]
        mutations = {
            "environment": "local-secure",
            "detector_id": "http-behavior-b",
            "target_type": "SESSION",
            "feature_version": "http-feat-9",
            "model_version": "http-if-9",
            "threshold_version": "http-thr-9",
            "input_hash": "sha256:" + "1" * 64,
        }
        seen = {doc["detection_id"]}
        for field, value in mutations.items():
            with self.subTest(field=field):
                derived = validate.derive_detection_id(dict(doc, **{field: value}))
                self.assertNotIn(derived, seen)
                seen.add(derived)
        for nested, value in (
            ("data_origin", {"kind": "SYNTHETIC_FIXTURE", "dataset_id": None}),
            ("target_key", {"key": "fx-subject-9999", "key_version": "fixture-v1"}),
            ("window", {"start": "2026-09-26T10:05:00Z", "end": "2026-09-26T10:10:00Z"}),
        ):
            with self.subTest(field=nested):
                derived = validate.derive_detection_id(dict(doc, **{nested: value}))
                self.assertNotIn(derived, seen)
                seen.add(derived)

    def test_valid_fixture_ids_are_unique(self) -> None:
        ids = [d["detection_id"] for d in self._valid_detections().values()]
        self.assertEqual(len(ids), len(set(ids)))


class ManifestTest(unittest.TestCase):
    def test_manifest_matches_current_files(self) -> None:
        manifest_path = CONTRACTS_DIR / "MANIFEST.json"
        self.assertTrue(manifest_path.exists(), "run: python contracts/tools/hash.py --write")
        self.assertEqual(manifest_path.read_text(encoding="utf-8"), manifest_tool.render(manifest_tool.build_manifest()))

    def test_manifest_covers_every_schema(self) -> None:
        manifest = manifest_tool.build_manifest()
        self.assertEqual(set(manifest["schemas"]), set(validate.SCHEMA_FILES))
        for version, rel in validate.SCHEMA_FILES.items():
            self.assertEqual(manifest["schemas"][version]["path"], rel)


class CliTest(unittest.TestCase):
    def test_cli_exit_codes(self) -> None:
        base = CONTRACTS_DIR / "security-event/v2/fixtures"
        self.assertEqual(validate.main([str(base / "valid/api_read_allow.json")]), 0)
        self.assertEqual(validate.main([str(base / "invalid/raw_jwt_in_field.json")]), 1)
        self.assertEqual(validate.main([str(base / "scenarios/api_edge_join.json")]), 0)
        self.assertEqual(validate.main([str(base / "scenarios/event_id_conflict.json")]), 1)
        self.assertEqual(validate.main(["--fixtures"]), 0)


if __name__ == "__main__":
    unittest.main()
