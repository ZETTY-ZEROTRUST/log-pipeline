"""indexer 항목별 결과 처리·poison→DLQ·receipt 멱등 unit test (fake Redis/ES/receipt)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from prometheus_client import CollectorRegistry

from pipeline.indexer.contract import EventClassifier, Rejected, default_contracts_dir, load_validator
from pipeline.indexer.es import BulkItemResult, BulkRequestError
from pipeline.indexer.indexer import Indexer, IndexerMetrics, IndexerSettings
from pipeline.indexer.streams import ClaimPage
from pipeline.tests.events import invalid_fixture, make_event

VALIDATOR = load_validator(default_contracts_dir())


class FakeConsumer:
    def __init__(self, entries=None) -> None:
        self.pending: dict[str, dict] = {}
        self.acked: list[str] = []
        self.dlq: list[dict] = []
        self.fail_dlq = False
        self.new = list(entries or [])
        self.last_delivered: str | None = None
        self.trimmed: list[str] = []
        self.trim_return = 0

    def ensure_group(self) -> None:
        pass

    def read_new(self, count, block_ms):
        batch, self.new = self.new[:count], self.new[count:]
        for sid, fields in batch:
            self.pending[sid] = fields
        return batch

    def read_own_pending(self, start, count):
        return [(sid, f) for sid, f in sorted(self.pending.items()) if sid > start][:count]

    def autoclaim(self, min_idle_ms, start, count):
        return ClaimPage("0-0", [(sid, f) for sid, f in sorted(self.pending.items())][:count], [])

    def ack(self, ids):
        for sid in ids:
            self.pending.pop(sid, None)
        self.acked.extend(ids)
        return len(ids)

    def dead_letter(self, rejected):
        if self.fail_dlq:
            raise ConnectionError("redis down")
        for r in rejected:
            self.dlq.append({"event_id": r.event_id, "rule": r.rule})

    def pending_count(self):
        return len(self.pending)

    def oldest_pending_id(self):
        return min(self.pending) if self.pending else None

    def last_delivered_id(self):
        return self.last_delivered

    def trim_min_id(self, min_id):
        self.trimmed.append(min_id)
        return self.trim_return


class FakeEs:
    """event_id별로 실패를 지정할 수 있는 bulk. 문서는 _id(event_id)로 덮어쓴다."""

    def __init__(self) -> None:
        self.docs: dict[str, tuple[str, dict]] = {}
        self.fail: dict[str, tuple[int, str]] = {}
        self.raise_error: BulkRequestError | None = None
        self.requests = 0

    def bulk(self, actions):
        self.requests += 1
        if self.raise_error:
            raise self.raise_error
        out = []
        for a in actions:
            if a.doc_id in self.fail:
                status, etype = self.fail[a.doc_id]
                out.append(BulkItemResult(False, status, etype))
            else:
                created = a.doc_id not in self.docs
                self.docs[a.doc_id] = (a.index, a.doc)
                out.append(BulkItemResult(True, 201 if created else 200, None))
        return out


class FakeReceipts:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, int]] = {}
        self.fail = False
        self.writes = 0

    def record(self, receipts):
        if self.fail:
            raise ConnectionError("mysql down")
        inserted = 0
        for event_id, index in receipts:
            self.writes += 1
            if event_id not in self.rows:  # INSERT IGNORE: 최초 값 유지
                self.rows[event_id] = (index, self.writes)
                inserted += 1
        return inserted


def entry(sid: str, doc) -> tuple[str, dict]:
    payload = doc if isinstance(doc, str) else json.dumps(doc)
    event_id = doc["event_id"] if isinstance(doc, dict) else "00000000-0000-4000-8000-00000000dead"
    return sid, {"event_id": event_id, "payload": payload}


@pytest.fixture()
def world():
    registry = CollectorRegistry()
    consumer, es, receipts = FakeConsumer(), FakeEs(), FakeReceipts()
    indexer = Indexer(
        consumer,
        EventClassifier(VALIDATOR),
        es,
        receipts,
        settings=IndexerSettings(batch_size=10, claim_idle_ms=1000),
        metrics=IndexerMetrics(registry),
    )
    return indexer, consumer, es, receipts, registry


def deliver(consumer: FakeConsumer, entries):
    for sid, fields in entries:
        consumer.pending[sid] = fields
    return entries


def test_all_items_ok_are_receipted_then_acked(world):
    indexer, consumer, es, receipts, registry = world
    docs = [make_event(kind=i) for i in range(3)]
    entries = deliver(consumer, [entry("1-%d" % i, d) for i, d in enumerate(docs)])
    result = indexer.process(entries)
    assert result.indexed == 3 and result.failed == 0
    assert consumer.acked == ["1-0", "1-1", "1-2"]
    assert set(receipts.rows) == {d["event_id"] for d in docs}
    assert consumer.pending_count() == 0
    assert registry.get_sample_value("zetty_indexer_indexed_total") == 3


def test_partial_bulk_failure_acks_only_successful_items(world):
    indexer, consumer, es, receipts, registry = world
    docs = [make_event(kind=i) for i in range(4)]
    es.fail[docs[1]["event_id"]] = (400, "index_closed_exception")
    es.fail[docs[3]["event_id"]] = (429, "es_rejected_execution_exception")
    entries = deliver(consumer, [entry("1-%d" % i, d) for i, d in enumerate(docs)])

    result = indexer.process(entries)

    assert result.indexed == 2 and result.failed == 2
    assert consumer.acked == ["1-0", "1-2"]
    assert set(consumer.pending) == {"1-1", "1-3"}  # 실패 항목은 pending으로 남는다
    assert set(receipts.rows) == {docs[0]["event_id"], docs[2]["event_id"]}
    assert registry.get_sample_value("zetty_indexer_failed_items_total", {"error_type": "index_closed_exception"}) == 1

    # 원인 해소 후 회수(XAUTOCLAIM)로 재처리하면 수렴한다.
    es.fail.clear()
    indexer.reclaim_idle()
    assert consumer.pending_count() == 0
    assert len(es.docs) == 4 and len(receipts.rows) == 4


def test_bulk_request_error_acks_nothing(world):
    indexer, consumer, es, receipts, registry = world
    es.raise_error = BulkRequestError("http 503")
    entries = deliver(consumer, [entry("1-0", make_event())])
    result = indexer.process(entries)
    assert consumer.acked == [] and receipts.rows == {}
    assert result.unacked == 1
    assert registry.get_sample_value("zetty_indexer_bulk_request_errors_total") == 1


def test_receipt_failure_after_es_write_acks_nothing_and_retry_does_not_duplicate(world):
    indexer, consumer, es, receipts, _ = world
    doc = make_event()
    entries = deliver(consumer, [entry("1-0", doc)])
    receipts.fail = True
    indexer.process(entries)
    assert consumer.acked == [] and len(es.docs) == 1  # ES에는 기록, ACK 없음

    receipts.fail = False
    indexer.reclaim_idle()
    assert consumer.acked == ["1-0"]
    assert len(es.docs) == 1 and len(receipts.rows) == 1  # 같은 _id 덮어쓰기, 중복 없음


def test_poison_goes_to_dlq_with_event_id_and_rule_only_then_acked(world):
    indexer, consumer, es, receipts, registry = world
    bad = invalid_fixture("raw_jwt_in_field.json")
    good = make_event()
    entries = deliver(consumer, [entry("1-0", bad), entry("1-1", good)])

    result = indexer.process(entries)

    assert result.dead_lettered == 1 and result.indexed == 1
    assert sorted(consumer.acked) == ["1-0", "1-1"]
    assert len(consumer.dlq) == 1
    dlq = consumer.dlq[0]
    assert set(dlq) == {"event_id", "rule"}
    assert dlq["event_id"] == bad["event_id"]
    assert dlq["rule"].startswith("schema:")
    raw = json.dumps(bad)
    assert "eyJ" in raw and all("eyJ" not in v for v in dlq.values())  # payload 원문이 DLQ로 가지 않는다
    assert bad["event_id"] not in es.docs and bad["event_id"] not in receipts.rows
    assert registry.get_sample_value("zetty_indexer_dlq_total", {"layer": "schema"}) == 1


def test_dlq_write_failure_does_not_ack_poison_but_good_items_proceed(world):
    indexer, consumer, es, receipts, _ = world
    consumer.fail_dlq = True
    entries = deliver(consumer, [entry("1-0", invalid_fixture("occurred_at_not_utc.json")), entry("1-1", make_event())])
    result = indexer.process(entries)
    assert consumer.acked == ["1-1"]
    assert "1-0" in consumer.pending
    assert result.unacked == 1


def test_duplicate_json_key_is_parse_poison(world):
    indexer, consumer, *_ = world
    text = invalid_fixture("duplicate_json_key.json")
    assert isinstance(text, str) or isinstance(text, dict)
    raw_text = (VALIDATOR.CONTRACTS_DIR / "security-event/v2/fixtures/invalid/duplicate_json_key.json").read_text()
    sid_fields = ("1-0", {"event_id": "00000000-0000-4000-8000-000000000001", "payload": raw_text})
    deliver(consumer, [sid_fields])
    indexer.process([sid_fields])
    assert consumer.dlq[0]["rule"] == "parse:DUPLICATE_JSON_KEY"


def test_envelope_event_id_mismatch_and_invalid_are_poison(world):
    indexer, consumer, *_ = world
    doc = make_event()
    other = make_event()
    entries = deliver(
        consumer,
        [
            ("1-0", {"event_id": other["event_id"], "payload": json.dumps(doc)}),
            ("1-1", {"event_id": "Bearer abc.def", "payload": json.dumps(doc)}),
            ("1-2", {"payload": json.dumps(doc)}),
        ],
    )
    indexer.process(entries)
    assert consumer.dlq == [
        {"event_id": other["event_id"], "rule": "envelope:ENVELOPE_EVENT_ID_MISMATCH"},
        {"event_id": "", "rule": "envelope:ENVELOPE_EVENT_ID_INVALID"},
        {"event_id": "", "rule": "envelope:ENVELOPE_MISSING_FIELD"},
    ]
    assert sorted(consumer.acked) == ["1-0", "1-1", "1-2"]


def test_oversized_payload_is_poison():
    classifier = EventClassifier(VALIDATOR, max_payload_bytes=1024)
    doc = make_event()
    result = classifier.classify("1-0", {"event_id": doc["event_id"], "payload": json.dumps(doc) + " " * 2000})
    assert isinstance(result, Rejected) and result.rule == "envelope:ENVELOPE_PAYLOAD_TOO_LARGE"


def test_receipt_is_idempotent_for_duplicate_publish(world):
    indexer, consumer, es, receipts, registry = world
    doc = make_event()
    first = deliver(consumer, [entry("1-0", doc)])
    indexer.process(first)
    first_write = receipts.rows[doc["event_id"]]
    second = deliver(consumer, [entry("2-0", doc), entry("2-1", doc)])  # relay 재발행으로 같은 event_id 두 번
    indexer.process(second)
    assert consumer.acked == ["1-0", "2-0", "2-1"]
    assert len(es.docs) == 1
    assert receipts.rows[doc["event_id"]] == first_write  # 최초 receipt 유지
    assert registry.get_sample_value("zetty_indexer_receipts_inserted_total") == 1


def test_index_is_chosen_by_occurred_at_utc_date_not_processing_time(world):
    indexer, consumer, es, receipts, _ = world
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    late = make_event(yesterday.replace(hour=23, minute=59, second=59, microsecond=999999))
    deliver(consumer, [entry("1-0", late)])
    indexer.process([entry("1-0", late)])
    expected = "zetty-security-events-v2-" + yesterday.strftime("%Y.%m.%d")
    assert es.docs[late["event_id"]][0] == expected
    assert receipts.rows[late["event_id"]][0] == expected


def test_failed_item_log_has_ids_and_type_only(world, caplog):
    indexer, consumer, es, *_ = world
    doc = make_event()
    es.fail[doc["event_id"]] = (400, "mapper_parsing_exception")
    with caplog.at_level(logging.WARNING, logger="pipeline.indexer"):
        indexer.process(deliver(consumer, [entry("1-0", doc)]))
    text = caplog.text
    assert doc["event_id"] in text and "mapper_parsing_exception" in text
    assert doc["actor"]["subject_key"] not in text  # 문서 값은 로그에 없다


def test_drain_own_pending_processes_entries_left_before_restart(world):
    indexer, consumer, es, receipts, _ = world
    docs = [make_event() for _ in range(3)]
    deliver(consumer, [entry("1-%d" % i, d) for i, d in enumerate(docs)])
    assert indexer.drain_own_pending() == 3
    assert consumer.pending_count() == 0 and len(es.docs) == 3 and len(receipts.rows) == 3


def test_all_valid_contract_fixtures_are_accepted_and_invalid_schema_fixtures_rejected():
    classifier = EventClassifier(VALIDATOR)
    index = json.loads((VALIDATOR.CONTRACTS_DIR / "security-event/v2/fixtures/index.json").read_text())
    base = VALIDATOR.CONTRACTS_DIR / "security-event/v2/fixtures"
    checked = 0
    for item in index["fixtures"]:
        if item.get("kind") == "scenario":
            continue
        text = (base / item["path"]).read_text(encoding="utf-8")
        try:
            event_id = json.loads(text).get("event_id")
        except ValueError:
            event_id = None
        fields = {"event_id": event_id if isinstance(event_id, str) else "", "payload": text}
        result = classifier.classify("1-0", fields)
        if item["expect"] == "valid":
            assert not isinstance(result, Rejected), item["path"]
        else:
            assert isinstance(result, Rejected), item["path"]
        checked += 1
    assert checked >= 40


def test_trim_uses_oldest_pending_as_floor(world):
    """pending이 있으면 그 최솟값을 트림 하한으로 써 미ACK 항목을 지키지 않는다."""
    indexer, consumer, es, receipts, registry = world
    consumer.pending = {"5-0": {}, "7-0": {}}
    consumer.last_delivered = "9-0"
    consumer.trim_return = 4
    trimmed = indexer.trim_indexed()
    assert consumer.trimmed == ["5-0"]  # 가장 오래된 pending 아래만 제거
    assert trimmed == 4
    assert registry.get_sample_value("zetty_indexer_stream_trimmed_total") == 4


def test_trim_falls_back_to_last_delivered_when_no_pending(world):
    """pending=0이면 last-delivered-id 이하는 모두 ACK된 것이라 그 지점까지 안전하게 트림한다."""
    indexer, consumer, es, receipts, registry = world
    consumer.pending = {}
    consumer.last_delivered = "42-0"
    consumer.trim_return = 41
    indexer.trim_indexed()
    assert consumer.trimmed == ["42-0"]


def test_trim_noop_when_stream_empty(world):
    """읽은 것도 pending도 없으면(하한 없음) 트림하지 않는다."""
    indexer, consumer, es, receipts, registry = world
    consumer.pending = {}
    consumer.last_delivered = None
    assert indexer.trim_indexed() == 0
    assert consumer.trimmed == []
