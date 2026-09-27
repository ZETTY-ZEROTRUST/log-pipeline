"""relay 루프: lease 획득(commit) → XADD → 내 lease인 행만 PUBLISHED."""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from pipeline.common.runtime import Backoff, Health, error_label, failpoint
from pipeline.relay.publisher import Publisher
from pipeline.relay.store import OutboxStore

LOG = logging.getLogger("pipeline.relay")

ATTEMPT_BUCKETS = (1, 2, 3, 5, 10, 20, 50)


def make_owner_id(hostname: str) -> str:
    """lease_owner(VARCHAR 64). 같은 호스트명의 재시작 프로세스끼리도 구분되도록 pid와 난수를 붙인다."""
    return "%s:%d:%s" % (hostname[:40], os.getpid(), secrets.token_hex(4))


class RelayMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.published = Counter("zetty_relay_published", "XADD에 성공한 Outbox 행 수(재발행 포함)", registry=registry)
        self.marked = Counter("zetty_relay_marked_published", "PUBLISHED로 표시한 행 수", registry=registry)
        self.publish_failures = Counter(
            "zetty_relay_publish_failures", "XADD가 실패한 행 수(lease 만료 후 재시도)", registry=registry
        )
        self.lease_lost = Counter(
            "zetty_relay_lease_lost", "XADD 후 lease를 잃어 표시하지 못한 행 수(중복 발행 가능)", registry=registry
        )
        self.attempts = Histogram(
            "zetty_relay_attempts", "lease 획득 시 행의 누적 attempts", buckets=ATTEMPT_BUCKETS, registry=registry
        )
        self.lag = Gauge("zetty_relay_lag_seconds", "now - 가장 오래된 PENDING 행의 occurred_at", registry=registry)
        self.pending = Gauge("zetty_relay_pending_rows", "PENDING 상태 Outbox 행 수", registry=registry)
        self.errors = Counter("zetty_relay_errors", "반복 실패 수", ["stage"], registry=registry)


@dataclass(frozen=True)
class RelaySettings:
    batch_size: int = 100
    poll_interval_s: float = 1.0
    lease_seconds: int = 30
    stats_interval_s: float = 5.0


class Relay:
    def __init__(
        self,
        store: OutboxStore,
        publisher: Publisher,
        *,
        owner: str,
        settings: RelaySettings,
        metrics: RelayMetrics,
        health: Health | None = None,
        clock=time.monotonic,
    ) -> None:
        self.store = store
        self.publisher = publisher
        self.owner = owner
        self.settings = settings
        self.metrics = metrics
        self.health = health
        self._clock = clock

    def run_once(self) -> int:
        """한 batch 처리. lease로 잡은 행 수를 반환한다."""
        rows = self.store.claim(self.owner, self.settings.batch_size, self.settings.lease_seconds)
        if not rows:
            return 0
        for row in rows:
            self.metrics.attempts.observe(row.attempts)
        # 여기서는 DB transaction이 이미 commit됐다. 네트워크 대기 동안 lock을 잡지 않는다.
        result = self.publisher.publish(rows)
        if result.failed:
            self.metrics.publish_failures.inc(result.failed)
            LOG.warning("xadd failed rows=%d; will retry after lease expiry", result.failed)
        if result.succeeded_ids:
            self.metrics.published.inc(len(result.succeeded_ids))
            failpoint("relay_after_xadd")
            marked = self.store.mark_published(self.owner, result.succeeded_ids)
            self.metrics.marked.inc(marked)
            lost = len(result.succeeded_ids) - marked
            if lost > 0:
                self.metrics.lease_lost.inc(lost)
                LOG.warning("lease lost rows=%d; duplicates are possible and idempotent by event_id", lost)
        return len(rows)

    def refresh_stats(self) -> None:
        stats = self.store.pending_stats()
        self.metrics.pending.set(stats.pending_rows)
        self.metrics.lag.set(stats.lag_seconds)

    def run_forever(self, stop: threading.Event) -> None:
        backoff = Backoff()
        last_stats = float("-inf")
        LOG.info(
            "relay started batch_size=%d poll_interval_s=%.3f lease_seconds=%d",
            self.settings.batch_size,
            self.settings.poll_interval_s,
            self.settings.lease_seconds,
        )
        while not stop.is_set():
            stage = "claim_publish"
            try:
                claimed = self.run_once()
                now = self._clock()
                if now - last_stats >= self.settings.stats_interval_s:
                    stage = "stats"
                    self.refresh_stats()
                    last_stats = now
                if self.health:
                    self.health.success()
                backoff.reset()
            except Exception as exc:  # noqa: BLE001 - 루프는 계속 돈다
                self.metrics.errors.labels(stage=stage).inc()
                if self.health:
                    self.health.beat()
                delay = backoff.next()
                LOG.warning("relay iteration failed stage=%s error=%s retry_in_s=%.1f", stage, error_label(exc), delay)
                stop.wait(delay)
                continue
            if claimed < self.settings.batch_size:
                stop.wait(self.settings.poll_interval_s)
        LOG.info("relay stopped")
