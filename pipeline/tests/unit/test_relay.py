"""relay lease/claim·발행 표시 로직 unit test (fake 저장소·fake Redis)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from prometheus_client import CollectorRegistry

from pipeline.relay.publisher import PublishResult, RedisStreamPublisher
from pipeline.relay.relay import Relay, RelayMetrics, RelaySettings, make_owner_id
from pipeline.relay.store import (
    CLAIM_SELECT_SQL,
    MARK_PUBLISHED_SQL,
    MySQLOutboxStore,
    OutboxRow,
    PendingStats,
)


@dataclass
class Row:
    id: int
    event_id: str
    payload: str
    status: str = "PENDING"
    lease_owner: str | None = None
    lease_until: float | None = None
    attempts: int = 0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeOutboxStore:
    """MySQL SQL과 같은 의미: PENDING이고 lease가 없거나 만료된 행을 id 순으로, 내 lease인 행만 PUBLISHED."""

    def __init__(self, clock: FakeClock, count: int) -> None:
        self.clock = clock
        self.rows = {i: Row(i, "evt-%d" % i, '{"n": %d}' % i) for i in range(1, count + 1)}
        self.in_transaction = False

    def claim(self, owner: str, batch_size: int, lease_seconds: int) -> list[OutboxRow]:
        self.in_transaction = True
        try:
            picked = [
                r
                for r in sorted(self.rows.values(), key=lambda r: r.id)
                if r.status == "PENDING" and (r.lease_until is None or r.lease_until < self.clock())
            ][:batch_size]
            for r in picked:
                r.lease_owner = owner
                r.lease_until = self.clock() + lease_seconds
                r.attempts += 1
            return [OutboxRow(r.id, r.event_id, r.payload, r.attempts) for r in picked]
        finally:
            self.in_transaction = False

    def mark_published(self, owner: str, ids) -> int:
        changed = 0
        for i in ids:
            r = self.rows[i]
            if r.lease_owner == owner and r.status == "PENDING":
                r.status = "PUBLISHED"
                changed += 1
        return changed

    def pending_stats(self) -> PendingStats:
        return PendingStats(sum(1 for r in self.rows.values() if r.status == "PENDING"), 0.0)


@dataclass
class FakeStream:
    entries: list[tuple[str, str]] = field(default_factory=list)
    fail_event_ids: set[str] = field(default_factory=set)
    store: FakeOutboxStore | None = None
    saw_open_transaction: bool = False

    def publish(self, rows) -> PublishResult:
        if self.store is not None and self.store.in_transaction:
            self.saw_open_transaction = True
        ok = []
        for row in rows:
            if row.event_id in self.fail_event_ids:
                continue
            self.entries.append((row.event_id, row.payload))
            ok.append(row.id)
        return PublishResult(ok, len(rows) - len(ok))


REGISTRIES: dict[int, CollectorRegistry] = {}


def make_relay(store, stream, owner, batch=3, lease=30) -> Relay:
    registry = CollectorRegistry()
    relay = Relay(
        store,
        stream,
        owner=owner,
        settings=RelaySettings(batch_size=batch, poll_interval_s=0.01, lease_seconds=lease),
        metrics=RelayMetrics(registry),
    )
    REGISTRIES[id(relay)] = registry
    return relay


def metric(relay: Relay, name: str) -> float:
    return REGISTRIES[id(relay)].get_sample_value("zetty_relay_%s_total" % name) or 0.0


def test_claims_pending_rows_in_id_order_up_to_batch_and_marks_published():
    clock = FakeClock()
    store = FakeOutboxStore(clock, 5)
    stream = FakeStream(store=store)
    relay = make_relay(store, stream, "relay-a", batch=3)

    assert relay.run_once() == 3
    assert [e[0] for e in stream.entries] == ["evt-1", "evt-2", "evt-3"]
    assert [store.rows[i].status for i in range(1, 6)] == ["PUBLISHED"] * 3 + ["PENDING"] * 2
    assert relay.run_once() == 2
    assert relay.run_once() == 0
    assert len(stream.entries) == 5
    assert all(r.attempts == 1 for r in store.rows.values())
    assert metric(relay, "published") == 5 and metric(relay, "marked_published") == 5
    assert not stream.saw_open_transaction


def test_two_relays_do_not_claim_the_same_leased_rows():
    clock = FakeClock()
    store = FakeOutboxStore(clock, 6)
    first = store.claim("relay-a", 4, 30)
    second = store.claim("relay-b", 4, 30)
    assert {r.id for r in first} == {1, 2, 3, 4}
    assert {r.id for r in second} == {5, 6}


def test_crash_after_xadd_before_mark_republishes_after_lease_expiry():
    clock = FakeClock()
    store = FakeOutboxStore(clock, 3)
    stream = FakeStream()
    # relay-a: lease 획득 → XADD 후 표시 전에 종료(표시 호출 없음)
    rows = store.claim("relay-a", 10, 30)
    stream.publish(rows)
    assert all(r.status == "PENDING" for r in store.rows.values())

    relay_b = make_relay(store, stream, "relay-b", batch=10, lease=30)
    assert relay_b.run_once() == 0  # lease가 살아 있는 동안은 다시 잡지 않는다
    clock.now += 31
    assert relay_b.run_once() == 3
    assert [e[0] for e in stream.entries] == ["evt-1", "evt-2", "evt-3"] * 2  # 중복 발행(event_id 동일)
    assert all(r.status == "PUBLISHED" and r.attempts == 2 for r in store.rows.values())


def test_only_successful_xadd_rows_are_marked_and_failed_row_is_retried():
    clock = FakeClock()
    store = FakeOutboxStore(clock, 3)
    stream = FakeStream(fail_event_ids={"evt-2"})
    relay = make_relay(store, stream, "relay-a", batch=10, lease=5)

    relay.run_once()
    assert store.rows[1].status == "PUBLISHED" and store.rows[3].status == "PUBLISHED"
    assert store.rows[2].status == "PENDING"
    assert metric(relay, "publish_failures") == 1

    stream.fail_event_ids.clear()
    assert relay.run_once() == 0  # 실패 행도 lease 만료 전에는 잡지 않는다
    clock.now += 6
    assert relay.run_once() == 1
    assert store.rows[2].status == "PUBLISHED" and store.rows[2].attempts == 2


def test_lease_lost_rows_are_not_marked_by_old_owner():
    clock = FakeClock()
    store = FakeOutboxStore(clock, 2)
    stream = FakeStream()
    slow = make_relay(store, stream, "relay-slow", batch=10, lease=5)
    rows = store.claim("relay-slow", 10, 5)
    clock.now += 6
    fast = make_relay(store, stream, "relay-fast", batch=10, lease=5)
    assert fast.run_once() == 2
    # 느린 relay가 뒤늦게 XADD하고 표시하려 하면 owner가 달라 0행
    stream.publish(rows)
    assert store.mark_published("relay-slow", [r.id for r in rows]) == 0
    assert all(r.lease_owner == "relay-fast" for r in store.rows.values())
    del slow


def test_owner_id_is_unique_and_fits_column():
    a = make_owner_id("x" * 200)
    b = make_owner_id("x" * 200)
    assert a != b
    assert len(a) <= 64


# ---------------------------------------------------------------- MySQL store: transaction 경계


class RecordingCursor:
    def __init__(self, conn) -> None:
        self.conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append(("execute", sql, params))
        if sql.startswith("SELECT id, event_id"):
            self._rows = self.conn.select_rows
            return len(self._rows)
        return len(params) - 1 if params else 0

    def fetchall(self):
        return self._rows


class RecordingConnection:
    def __init__(self, select_rows) -> None:
        self.calls: list[tuple] = []
        self.select_rows = select_rows

    def begin(self):
        self.calls.append(("begin",))

    def commit(self):
        self.calls.append(("commit",))

    def rollback(self):
        self.calls.append(("rollback",))

    def cursor(self):
        return RecordingCursor(self)

    def close(self):
        self.calls.append(("close",))


class StaticDb:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.discarded = 0

    def get(self):
        return self.conn

    def discard(self):
        self.discarded += 1


def test_mysql_claim_commits_before_returning_so_publish_runs_without_db_lock():
    conn = RecordingConnection([(7, "evt-7", '{"a":1}', 0), (9, "evt-9", '{"a":2}', 2)])
    store = MySQLOutboxStore(StaticDb(conn))

    class AssertingPublisher:
        calls_at_publish: list = []

        def publish(self, rows):
            AssertingPublisher.calls_at_publish = list(conn.calls)
            return PublishResult([r.id for r in rows], 0)

    relay = Relay(
        store,
        AssertingPublisher(),
        owner="relay-a",
        settings=RelaySettings(batch_size=10, lease_seconds=30),
        metrics=RelayMetrics(CollectorRegistry()),
    )
    relay.run_once()
    kinds = [c[0] for c in AssertingPublisher.calls_at_publish]
    assert kinds == ["begin", "execute", "execute", "commit"]  # XADD 시점에는 transaction이 끝나 있다
    select_sql = AssertingPublisher.calls_at_publish[1][1]
    assert select_sql == CLAIM_SELECT_SQL
    assert "FOR UPDATE SKIP LOCKED" in select_sql and "ORDER BY id" in select_sql
    update_params = AssertingPublisher.calls_at_publish[2][2]
    assert update_params == ("relay-a", 30, 7, 9)
    mark = conn.calls[-1]
    assert mark[1] == MARK_PUBLISHED_SQL.format(ids="%s, %s")
    assert "lease_owner = %s AND status = 'PENDING'" in mark[1]
    assert mark[2] == (7, 9, "relay-a")


def test_mysql_claim_rolls_back_and_discards_connection_on_error():
    class Boom(RecordingConnection):
        def cursor(self):
            raise RuntimeError("db down")

    conn = Boom([])
    db = StaticDb(conn)
    with pytest.raises(RuntimeError):
        MySQLOutboxStore(db).claim("relay-a", 10, 30)
    assert ("rollback",) in conn.calls
    assert db.discarded == 1


def test_attempts_are_reported_after_increment():
    conn = RecordingConnection([(1, "evt-1", "{}", 4)])
    rows = MySQLOutboxStore(StaticDb(conn)).claim("relay-a", 10, 30)
    assert rows[0].attempts == 5


# ---------------------------------------------------------------- Redis publisher


class FakePipeline:
    def __init__(self, fail_index: int | None) -> None:
        self.commands = []
        self.fail_index = fail_index

    def xadd(self, stream, fields):
        self.commands.append((stream, dict(fields)))

    def execute(self, raise_on_error=True):
        assert raise_on_error is False
        return [ValueError("x") if i == self.fail_index else "1-%d" % i for i in range(len(self.commands))]


class FakeRedis:
    def __init__(self, fail_index=None) -> None:
        self.pipe = FakePipeline(fail_index)

    def pipeline(self, transaction=True):
        assert transaction is False
        return self.pipe


def test_publisher_sends_event_id_and_payload_and_reports_item_failures():
    client = FakeRedis(fail_index=1)
    rows = [OutboxRow(1, "evt-1", '{"x":1}', 1), OutboxRow(2, "evt-2", '{"x":2}', 1), OutboxRow(3, "evt-3", "{}", 1)]
    result = RedisStreamPublisher(client, "zetty:security-events").publish(rows)
    assert result.succeeded_ids == [1, 3]
    assert result.failed == 1
    assert client.pipe.commands[0] == ("zetty:security-events", {"event_id": "evt-1", "payload": '{"x":1}'})
