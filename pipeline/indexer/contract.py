"""C-02 SecurityEvent v2 계약으로 stream 항목을 분류한다.

검증 규칙은 `contracts/tools/validate.py`를 그대로 불러 쓴다(복사·재구현하지 않는다).
poison 사유에는 규칙 ID와 JSON pointer만 남기고 payload 값은 남기지 않는다.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

SECURITY_EVENT_VERSION = "security-event/2.0"
SECURITY_EVENT_SCHEMA = "security-event/v2/schema.json"
DEFAULT_INDEX_PREFIX = "zetty-security-events-v2-"
ES_INDEX_MAX_LEN = 64  # security_event_receipt.es_index VARCHAR(64)

_CANONICAL_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_MAX_RULE_LEN = 256


def default_contracts_dir() -> Path:
    env = os.environ.get("CONTRACTS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "contracts"


def load_validator(contracts_dir: Path) -> ModuleType:
    path = contracts_dir / "tools" / "validate.py"
    spec = importlib.util.spec_from_file_location("zetty_contract_validate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("contract validator not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class ContractInfo:
    revision: str
    schema_sha256: str


def verify_contract_snapshot(contracts_dir: Path) -> ContractInfo:
    """배포된 schema 파일이 MANIFEST.json에 고정된 hash와 같은지 확인한다."""
    manifest = json.loads((contracts_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    expected = manifest["schemas"][SECURITY_EVENT_VERSION]["sha256"]
    actual = hashlib.sha256((contracts_dir / SECURITY_EVENT_SCHEMA).read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError("security-event schema sha256 does not match contracts/MANIFEST.json")
    return ContractInfo(revision=manifest["revision"], schema_sha256=actual)


@dataclass(frozen=True)
class Accepted:
    stream_id: str
    event_id: str
    index: str
    doc: dict[str, Any]


@dataclass(frozen=True)
class Rejected:
    stream_id: str
    event_id: str  # canonical UUID가 아니면 빈 문자열(원문을 DLQ에 옮기지 않는다)
    rule: str  # "<layer>:<규칙 ID 목록>"
    layer: str


class EventClassifier:
    def __init__(
        self,
        validator: ModuleType,
        *,
        index_prefix: str = DEFAULT_INDEX_PREFIX,
        max_payload_bytes: int = 65536,
    ) -> None:
        if len(index_prefix) + len("YYYY.MM.DD") > ES_INDEX_MAX_LEN:
            raise ValueError("index prefix too long for security_event_receipt.es_index")
        self._v = validator
        self._prefix = index_prefix
        self._max_payload = max_payload_bytes
        # format checker(uuid/date-time)가 꺼진 채 통과하는 일이 없도록 기동 시 확인한다.
        self._v.get_validator(SECURITY_EVENT_SCHEMA)

    def index_name(self, occurred_at: str) -> str:
        """UTC occurred_at 날짜(처리 시각이 아님)."""
        return self._prefix + self._v.parse_utc(occurred_at).strftime("%Y.%m.%d")

    def classify(self, stream_id: str, fields: Mapping[str, Any] | None) -> Accepted | Rejected:
        if fields is None:
            return Rejected(stream_id, "", "envelope:ENVELOPE_ENTRY_DELETED", "envelope")
        raw_id = fields.get("event_id")
        payload = fields.get("payload")
        event_id = raw_id if isinstance(raw_id, str) and _CANONICAL_UUID.match(raw_id) else ""
        if not isinstance(raw_id, str) or not isinstance(payload, str):
            return self._reject(stream_id, event_id, "envelope", "ENVELOPE_MISSING_FIELD")
        if not event_id:
            return self._reject(stream_id, "", "envelope", "ENVELOPE_EVENT_ID_INVALID")
        if len(payload.encode("utf-8")) > self._max_payload:
            return self._reject(stream_id, event_id, "envelope", "ENVELOPE_PAYLOAD_TOO_LARGE")
        try:
            doc = self._v.loads_strict(payload)
        except self._v.ContractParseError as exc:
            return self._reject(stream_id, event_id, "parse", str(exc))
        except (ValueError, RecursionError):
            return self._reject(stream_id, event_id, "parse", "INVALID_JSON")
        violations = self._v.validate_document(doc, SECURITY_EVENT_SCHEMA)
        if violations:
            rules = ",".join(sorted({v.rule for v in violations}))
            return self._reject(stream_id, event_id, violations[0].layer, rules)
        if doc["event_id"] != event_id:
            return self._reject(stream_id, event_id, "envelope", "ENVELOPE_EVENT_ID_MISMATCH")
        return Accepted(stream_id, event_id, self.index_name(doc["occurred_at"]), doc)

    @staticmethod
    def _reject(stream_id: str, event_id: str, layer: str, rule: str) -> Rejected:
        return Rejected(stream_id, event_id, ("%s:%s" % (layer, rule))[:_MAX_RULE_LEN], layer)
