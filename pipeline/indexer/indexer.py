"""indexer 루프.

순서: 분류(C-02) → poison은 DLQ 후 ACK → 정상 항목 ES bulk → 항목별 성공만 receipt → 그 stream ID만 XACK.
실패 항목은 pending(PEL)으로 남아 XAUTOCLAIM으로 다시 처리된다. ES 문서 _id가 event_id라서
ACK 전 종료 뒤 재처리해도 문서가 늘지 않고, receipt는 INSERT IGNORE로 한 번만 남는다.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Sequence

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from pipeline.common.runtime import Backoff, Health, error_label, failpoint
from pipeline.indexer.contract import Accepted, EventClassifier, Rejected
from pipeline.indexer.es import BulkAction, BulkClient, BulkRequestError
from pipeline.indexer.receipts import ReceiptStore
from pipeline.indexer.streams import Consumer, Entry, is_nogroup

LOG = logging.getLogger("pipeline.indexer")


class IndexerMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.indexed = Counter(
            "zetty_indexer_indexed", "ES 항목 성공 + receipt 기록 + XACK까지 끝난 항목 수", registry=registry
        )
        self.failed_items = Counter(
            "zetty_indexer_failed_items", "ES bulk 항목 실패 수(ACK 안 함)", ["error_type"], registry=registry
        )
        self.bulk_errors = Counter(
            "zetty_indexer_bulk_request_errors", "bulk 요청 전체 실패 수(ACK 안 함)", registry=registry
        )
        self.dlq = Counter("zetty_indexer_dlq", "DLQ로 격리하고 ACK한 항목 수", ["layer"], registry=registry)
        self.receipts_inserted = Counter(
            "zetty_indexer_receipts_inserted", "새로 기록된 receipt 행 수(중복 무시 제외)", registry=registry
        )
        self.reclaimed = Counter("zetty_indexer_reclaimed", "XAUTOCLAIM으로 회수한 항목 수", registry=registry)
        self.pending = Gauge("zetty_indexer_pending", "consumer group pending(ACK 전) 항목 수", registry=registry)
        self.errors = Counter("zetty_indexer_errors", "반복 실패 수", ["stage"], registry=registry)
        self.bulk_seconds = Histogram("zetty_indexer_bulk_seconds", "ES bulk 요청 시간", registry=registry)


@dataclass(frozen=True)
class IndexerSettings:
    batch_size: int = 100
    block_ms: int = 2000
    claim_idle_ms: int = 60000
    claim_interval_s: float = 15.0
    claim_max_batches: int = 10


@dataclass
class BatchResult:
    acked: list[str] = field(default_factory=list)
    indexed: int = 0
    failed: int = 0
    dead_lettered: int = 0
    unacked: int = 0


class Indexer:
    def __init__(
        self,
        consumer: Consumer,
        classifier: EventClassifier,
        es: BulkClient,
        receipts: ReceiptStore,
        *,
        settings: IndexerSettings,
        metrics: IndexerMetrics,
        health: Health | None = None,
        clock=time.monotonic,
    ) -> None:
        self.consumer = consumer
        self.classifier = classifier
        self.es = es
        self.receipts = receipts
        self.settings = settings
        self.metrics = metrics
        self.health = health
        self._clock = clock

    # ------------------------------------------------------------ batch

    def process(self, entries: Sequence[Entry]) -> BatchResult:
        result = BatchResult()
        accepted: list[Accepted] = []
        rejected: list[Rejected] = []
        for stream_id, fields in entries:
            item = self.classifier.classify(stream_id, fields)
            (accepted if isinstance(item, Accepted) else rejected).append(item)
        if rejected:
            self._dead_letter(rejected, result)
        if accepted:
            self._index(accepted, result)
        return result

    def _dead_letter(self, rejected: list[Rejected], result: BatchResult) -> None:
        try:
            self.consumer.dead_letter(rejected)
            ids = [r.stream_id for r in rejected]
            self.consumer.ack(ids)
        except Exception as exc:  # noqa: BLE001 - DLQ 실패는 ACK하지 않고 다음 회수에서 재시도
            self.metrics.errors.labels(stage="dlq").inc()
            result.unacked += len(rejected)
            LOG.warning("dlq write failed items=%d error=%s", len(rejected), error_label(exc))
            return
        result.acked.extend(ids)
        result.dead_lettered += len(rejected)
        for r in rejected:
            self.metrics.dlq.labels(layer=r.layer).inc()
            LOG.warning("poison event quarantined event_id=%s rule=%s", r.event_id or "-", r.rule)

    def _index(self, accepted: list[Accepted], result: BatchResult) -> None:
        actions = [BulkAction(a.index, a.event_id, a.doc) for a in accepted]
        started = self._clock()
        try:
            items = self.es.bulk(actions)
        except BulkRequestError as exc:
            self.metrics.bulk_errors.inc()
            result.unacked += len(accepted)
            LOG.warning("bulk request failed items=%d error=%s", len(accepted), exc)
            return
        finally:
            self.metrics.bulk_seconds.observe(max(0.0, self._clock() - started))

        succeeded: list[Accepted] = []
        for entry, item in zip(accepted, items, strict=True):
            if item.ok:
                succeeded.append(entry)
                continue
            result.failed += 1
            result.unacked += 1
            self.metrics.failed_items.labels(error_type=item.error_type or "unknown").inc()
            LOG.warning(
                "es item failed event_id=%s index=%s status=%d error_type=%s; left pending",
                entry.event_id,
                entry.index,
                item.status,
                item.error_type,
            )
        failpoint("indexer_after_bulk")
        if not succeeded:
            return
        try:
            inserted = self.receipts.record([(a.event_id, a.index) for a in succeeded])
        except Exception as exc:  # noqa: BLE001 - receipt 전에는 ACK하지 않는다
            self.metrics.errors.labels(stage="receipt").inc()
            result.unacked += len(succeeded)
            LOG.warning("receipt write failed items=%d error=%s", len(succeeded), error_label(exc))
            return
        self.metrics.receipts_inserted.inc(inserted)
        failpoint("indexer_after_receipt")
        ids = [a.stream_id for a in succeeded]
        try:
            self.consumer.ack(ids)
        except Exception as exc:  # noqa: BLE001 - 재처리는 멱등
            self.metrics.errors.labels(stage="ack").inc()
            result.unacked += len(succeeded)
            LOG.warning("xack failed items=%d error=%s", len(succeeded), error_label(exc))
            return
        result.acked.extend(ids)
        result.indexed += len(succeeded)
        self.metrics.indexed.inc(len(succeeded))

    # ------------------------------------------------------------ recovery reads

    def drain_own_pending(self) -> int:
        """재기동 직후: 같은 consumer 이름으로 받았던 미ACK 항목을 먼저 처리한다."""
        start, handled = "0", 0
        while True:
            entries = self.consumer.read_own_pending(start, self.settings.batch_size)
            if not entries:
                return handled
            self.process(entries)
            handled += len(entries)
            start = entries[-1][0]

    def reclaim_idle(self) -> int:
        """다른(종료된) consumer나 실패로 남은 idle 항목을 가져와 처리한다."""
        start, handled = "0-0", 0
        for _ in range(self.settings.claim_max_batches):
            page = self.consumer.autoclaim(self.settings.claim_idle_ms, start, self.settings.batch_size)
            if page.deleted_ids:
                LOG.warning("pending entries deleted from stream count=%d", len(page.deleted_ids))
            if page.entries:
                self.metrics.reclaimed.inc(len(page.entries))
                self.process(page.entries)
                handled += len(page.entries)
            start = page.next_start
            if start in ("0-0", "0"):
                break
        return handled

    # ------------------------------------------------------------ loop

    def run_forever(self, stop: threading.Event) -> None:
        backoff = Backoff()
        started = False
        last_claim = float("-inf")
        LOG.info(
            "indexer started batch_size=%d block_ms=%d claim_idle_ms=%d claim_interval_s=%.1f",
            self.settings.batch_size,
            self.settings.block_ms,
            self.settings.claim_idle_ms,
            self.settings.claim_interval_s,
        )
        while not stop.is_set():
            stage = "startup"
            try:
                if not started:
                    self.consumer.ensure_group()
                    drained = self.drain_own_pending()
                    if drained:
                        LOG.info("drained own pending entries=%d", drained)
                    started = True
                now = self._clock()
                if now - last_claim >= self.settings.claim_interval_s:
                    stage = "reclaim"
                    self.reclaim_idle()
                    self.metrics.pending.set(self.consumer.pending_count())
                    last_claim = now
                stage = "read"
                entries = self.consumer.read_new(self.settings.batch_size, self.settings.block_ms)
                if entries:
                    self.process(entries)
                if self.health:
                    self.health.success()
                backoff.reset()
            except Exception as exc:  # noqa: BLE001 - 루프는 계속 돈다
                if is_nogroup(exc):
                    # Redis 유실(FLUSHALL·새 인스턴스): group을 0부터 다시 만든다.
                    LOG.warning("consumer group missing; recreating stage=%s", stage)
                    started = False
                    stop.wait(0.2)
                    continue
                self.metrics.errors.labels(stage=stage).inc()
                if self.health:
                    self.health.beat()
                delay = backoff.next()
                LOG.warning(
                    "indexer iteration failed stage=%s error=%s retry_in_s=%.1f", stage, error_label(exc), delay
                )
                stop.wait(delay)
        LOG.info("indexer stopped")
