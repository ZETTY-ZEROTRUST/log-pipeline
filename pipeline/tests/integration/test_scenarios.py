"""Outbox → Redis Streams → ES 복구 시나리오(a~g)와 최소 권한 확인.

각 시나리오는 기대/실측 건수를 lab.record()로 남기고 run.sh가 JSON 보고서로 모은다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pymysql
import pytest
import redis

from pipeline.tests.events import format_utc, invalid_fixture, make_event
from pipeline.tests.integration.lab import DLQ, STREAM, Lab, new_id, wait_until

pytestmark = pytest.mark.integration

RELAY_METRICS_PORT = 9101
INDEXER_METRICS_PORT = 9102


def events(n: int, when: datetime | None = None) -> list[dict]:
    base = when or datetime.now(timezone.utc)
    return [make_event(base - timedelta(milliseconds=i), kind=i) for i in range(n)]


def index_for(when: datetime) -> str:
    return "security-events-v2-" + when.strftime("%Y.%m.%d")


def converged(lab: Lab, n: int):
    def check() -> bool:
        return lab.receipts() >= n and lab.pending() == 0

    return check


def assert_exact(lab: Lab, docs: list[dict]) -> None:
    hits = lab.es_docs()
    ids = [h["_id"] for h in hits]
    assert len(ids) == len(docs), "duplicate or missing documents"
    assert set(ids) == {d["event_id"] for d in docs}


# ---------------------------------------------------------------- (a) 정상 경로


def test_a_happy_path_two_relays_two_indexers(fresh: Lab):
    lab = fresh
    n = 200
    docs = events(n)
    lab.run_relay("relay-1")
    lab.run_relay("relay-2")
    idx1 = lab.run_indexer("indexer-1")
    lab.run_indexer("indexer-2")
    for chunk in range(0, n, 50):
        lab.insert_events(docs[chunk : chunk + 50])
    wait_until(converged(lab, n), 90, what="a converge")

    c = lab.counts()
    assert_exact(lab, docs)
    attempts = lab.sql("SELECT attempts, COUNT(*) FROM security_event_outbox GROUP BY attempts")
    by_id = {h["_id"]: h["_source"] for h in lab.es_docs()}
    content_equal = sum(1 for d in docs if by_id.get(d["event_id"]) == d)
    relay_published = sum(
        lab.metrics("%s-%s" % (lab.run_id, name), RELAY_METRICS_PORT).get("zetty_relay_published_total", 0)
        for name in ("relay-1", "relay-2")
    )
    indexer_metrics = lab.metrics(idx1, INDEXER_METRICS_PORT)
    relay_metrics = lab.metrics("%s-relay-1" % lab.run_id, RELAY_METRICS_PORT)
    mapping = lab.es("GET", "/" + index_for(datetime.now(timezone.utc)) + "/_mapping").json()
    dynamic = {m["mappings"].get("dynamic") for m in mapping.values()}
    lab.record(
        "a_happy_path",
        {"es_docs": n, "receipts": n, "outbox_published": n, "stream_len": n, "pending": 0, "content_equal": n},
        {
            "es_docs": c.es_docs,
            "receipts": c.receipts,
            "outbox_published": c.outbox_published,
            "stream_len": c.stream_len,
            "pending": c.pending,
            "content_equal": content_equal,
            "attempts_histogram": {int(a): int(k) for a, k in attempts},
            "relay_published_metric_sum": relay_published,
            "index_mapping_dynamic": sorted(dynamic),
        },
        note="relay 2 + indexer 2 동시 실행",
    )
    assert dynamic == {"strict"}  # indexer가 기동 시 설치한 template이 적용됨
    assert "zetty_relay_lag_seconds" in relay_metrics and 'zetty_relay_attempts_bucket{le="1.0"}' in relay_metrics
    assert c.es_docs == n and c.receipts == n and c.outbox_published == n
    assert c.stream_len == n  # 정상 운전에서는 SKIP LOCKED lease로 중복 발행 0
    assert c.pending == 0 and c.dlq_len == 0
    assert content_equal == n
    assert {int(a) for a, _ in attempts} == {1}
    assert relay_published == n
    assert "zetty_indexer_indexed_total" in indexer_metrics


# ---------------------------------------------------------------- (b) 중복 발행


def test_b_duplicate_publish_after_relay_crash_before_mark(fresh: Lab):
    lab = fresh
    n, batch = 60, 20
    docs = events(n)
    lab.insert_events(docs)
    crashed = lab.run_relay(
        "relay-crash", PIPELINE_FAILPOINT="relay_after_xadd", RELAY_BATCH_SIZE=str(batch), RELAY_LEASE_SECONDS="3"
    )
    exit_code = lab.wait_exit(crashed)
    after_crash = lab.counts()
    assert exit_code == 86
    assert after_crash.stream_len == batch and after_crash.outbox_pending == n

    lab.run_indexer("indexer-1")
    relay2 = lab.run_relay("relay-2", RELAY_BATCH_SIZE=str(batch), RELAY_LEASE_SECONDS="3")
    wait_until(lambda: converged(lab, n)() and lab.counts().outbox_published == n, 90, what="b converge")
    m2 = lab.metrics(relay2, RELAY_METRICS_PORT)
    histogram = {le: m2.get('zetty_relay_attempts_bucket{le="%s"}' % le) for le in ("1.0", "2.0")}

    c = lab.counts()
    assert_exact(lab, docs)
    attempts = dict(lab.sql("SELECT attempts, COUNT(*) FROM security_event_outbox GROUP BY attempts"))
    lab.record(
        "b_duplicate_publish",
        {"stream_len": n + batch, "es_docs": n, "receipts": n, "rows_attempts_2": batch},
        {
            "relay_crash_exit": exit_code,
            "stream_len": c.stream_len,
            "es_docs": c.es_docs,
            "es_unique_ids": c.es_unique_ids,
            "receipts": c.receipts,
            "rows_attempts_2": int(attempts.get(2, 0)),
            "pending": c.pending,
            "relay2_attempts_histogram_cumulative": histogram,
        },
        note="relay가 XADD 후 PUBLISHED 표시 전에 종료 → lease(3s) 만료 후 다른 relay가 재발행",
    )
    assert c.stream_len == n + batch
    assert c.es_docs == n and c.receipts == n and c.pending == 0
    assert int(attempts.get(2, 0)) == batch
    assert histogram == {"1.0": n - batch, "2.0": n}


# ---------------------------------------------------------------- (c) ACK 전 종료


def _crash_indexer_mid_batch(lab: Lab, docs: list[dict], failpoint: str, hostname: str) -> dict:
    n, batch = len(docs), 25
    lab.run_relay("relay-1")
    lab.insert_events(docs)
    wait_until(lambda: lab.counts().outbox_published == n, 60, what="published")
    crashed = lab.run_indexer(
        "indexer-crash", hostname=hostname, PIPELINE_FAILPOINT=failpoint, INDEXER_BATCH_SIZE=str(batch)
    )
    exit_code = lab.wait_exit(crashed)
    c = lab.counts()
    return {"exit": exit_code, "es_docs": c.es_docs, "receipts": c.receipts, "pending": c.pending}


def test_c1_crash_after_es_write_before_ack_restart_same_consumer(fresh: Lab):
    lab = fresh
    n = 60
    docs = events(n)
    crash = _crash_indexer_mid_batch(lab, docs, "indexer_after_bulk", hostname="idx-a")
    assert crash == {"exit": 86, "es_docs": 25, "receipts": 0, "pending": 25}

    lab.run_indexer("indexer-restart", hostname="idx-a")  # 같은 consumer 이름 → 기동 시 자기 PEL부터 처리
    wait_until(converged(lab, n), 60, what="c1 converge")
    c = lab.counts()
    assert_exact(lab, docs)
    reindexed = sum(1 for h in lab.es_docs() if h["_version"] > 1)
    lab.record(
        "c1_crash_after_bulk_same_consumer",
        {
            "es_docs_after_crash": 25,
            "receipts_after_crash": 0,
            "pending_after_crash": 25,
            "es_docs": n,
            "receipts": n,
            "pending": 0,
            "docs_version_2": 25,
        },
        {
            "crash": crash,
            "es_docs": c.es_docs,
            "es_unique_ids": c.es_unique_ids,
            "receipts": c.receipts,
            "pending": c.pending,
            "docs_version_2": reindexed,
        },
        note="bulk 성공 직후 os._exit(86) → 같은 hostname으로 재기동, XREADGROUP 0 으로 자기 PEL 처리",
    )
    assert c.es_docs == n and c.receipts == n and c.pending == 0
    assert reindexed == 25  # 같은 _id 덮어쓰기(새 문서가 아니라 version 증가)


def test_c2_crash_after_receipt_before_ack_other_consumer_autoclaim(fresh: Lab):
    lab = fresh
    n = 60
    docs = events(n)
    crash = _crash_indexer_mid_batch(lab, docs, "indexer_after_receipt", hostname="idx-b")
    assert crash == {"exit": 86, "es_docs": 25, "receipts": 25, "pending": 25}

    other = lab.run_indexer("indexer-other", hostname="idx-c")  # 다른 consumer → idle 3s 후 XAUTOCLAIM
    wait_until(converged(lab, n), 60, what="c2 converge")
    c = lab.counts()
    assert_exact(lab, docs)
    m = lab.metrics(other, INDEXER_METRICS_PORT)
    lab.record(
        "c2_crash_after_receipt_autoclaim",
        {
            "es_docs_after_crash": 25,
            "receipts_after_crash": 25,
            "pending_after_crash": 25,
            "es_docs": n,
            "receipts": n,
            "pending": 0,
            "reclaimed": 25,
            "receipts_inserted_by_other": n - 25,
        },
        {
            "crash": crash,
            "es_docs": c.es_docs,
            "es_unique_ids": c.es_unique_ids,
            "receipts": c.receipts,
            "pending": c.pending,
            "reclaimed": m.get("zetty_indexer_reclaimed_total"),
            "receipts_inserted_by_other": m.get("zetty_indexer_receipts_inserted_total"),
        },
        note="receipt 기록 직후 os._exit(86) → 다른 consumer가 XAUTOCLAIM(idle>3000ms)으로 회수",
    )
    assert c.es_docs == n and c.receipts == n and c.pending == 0
    assert m.get("zetty_indexer_reclaimed_total") == 25
    assert m.get("zetty_indexer_receipts_inserted_total") == n - 25  # receipt 중복 0


# ---------------------------------------------------------------- (d) ES 부분 실패


def test_d_es_partial_failure_only_successes_acked(fresh: Lab):
    lab = fresh
    now = datetime.now(timezone.utc)
    closed_day, blocked_day = now - timedelta(days=2), now - timedelta(days=3)
    ok_docs, closed_docs, blocked_docs = events(20, now), events(20, closed_day), events(20, blocked_day)
    closed_index, blocked_index = index_for(closed_day), index_for(blocked_day)
    template = json.loads((lab_template_path()).read_text(encoding="utf-8"))
    lab.es("PUT", "/_index_template/security-events-v2", json=template).raise_for_status()
    lab.es("PUT", "/" + closed_index).raise_for_status()
    lab.es("POST", "/" + closed_index + "/_close").raise_for_status()
    lab.es("PUT", "/" + blocked_index).raise_for_status()
    lab.es("PUT", "/" + blocked_index + "/_block/write").raise_for_status()

    lab.run_relay("relay-1")
    idx = lab.run_indexer("indexer-1")
    lab.insert_events(ok_docs + closed_docs + blocked_docs)
    wait_until(lambda: lab.receipts() == 20 and lab.pending() == 40, 60, what="d partial")
    wait_until(lambda: lab.metrics(idx, INDEXER_METRICS_PORT).get("zetty_indexer_reclaimed_total", 0) >= 40, 30)
    partial = lab.counts()
    pending_ids = lab.pending_event_ids()
    m = lab.metrics(idx, INDEXER_METRICS_PORT)
    failed_closed = m.get('zetty_indexer_failed_items_total{error_type="index_closed_exception"}', 0)
    failed_blocked = m.get('zetty_indexer_failed_items_total{error_type="cluster_block_exception"}', 0)
    receipt_ids = {r[0] for r in lab.sql("SELECT event_id FROM security_event_receipt")}
    assert receipt_ids == {d["event_id"] for d in ok_docs}
    assert pending_ids == {d["event_id"] for d in closed_docs + blocked_docs}

    lab.es("POST", "/" + closed_index + "/_open").raise_for_status()
    lab.es("PUT", "/" + blocked_index + "/_settings", json={"index.blocks.write": False}).raise_for_status()
    wait_until(converged(lab, 60), 60, what="d converge")
    c = lab.counts()
    assert_exact(lab, ok_docs + closed_docs + blocked_docs)
    lab.record(
        "d_es_partial_failure",
        {
            "receipts_during_failure": 20,
            "pending_during_failure": 40,
            "pending_ids_match_failed": True,
            "es_docs_after_fix": 60,
            "receipts_after_fix": 60,
            "pending_after_fix": 0,
        },
        {
            "receipts_during_failure": partial.receipts,
            "pending_during_failure": partial.pending,
            "es_docs_during_failure": partial.es_docs,
            "pending_ids_match_failed": True,
            "failed_items_index_closed": failed_closed,
            "failed_items_cluster_block": failed_blocked,
            "es_docs_after_fix": c.es_docs,
            "receipts_after_fix": c.receipts,
            "pending_after_fix": c.pending,
        },
        note="%s 닫힘(400), %s write block(403); 실패 항목은 XAUTOCLAIM으로 반복 재시도"
        % (closed_index, blocked_index),
    )
    assert partial.receipts == 20 and partial.pending == 40
    assert failed_closed >= 20 and failed_blocked >= 20
    assert c.es_docs == 60 and c.receipts == 60 and c.pending == 0


def lab_template_path():
    from pipeline.indexer.__main__ import DEFAULT_TEMPLATE_PATH

    return DEFAULT_TEMPLATE_PATH


# ---------------------------------------------------------------- (e) Redis 유실


def _recovery_value(stdout: str, key: str) -> int:
    for token in stdout.split():
        if token.startswith(key + "="):
            return int(token.split("=", 1)[1])
    raise AssertionError("%s not in recovery output" % key)


def test_e_redis_flushall_then_replay_from_outbox(fresh: Lab):
    lab = fresh
    n1, n2, n3 = 40, 40, 20
    first, second, third = events(n1), events(n2), events(n3)
    lab.run_relay("relay-1")
    idx = lab.run_indexer("indexer-1")
    lab.insert_events(first)
    wait_until(converged(lab, n1), 60, what="e first")

    lab.stop(idx)  # 소비 중단: 다음 이벤트는 Redis에만 존재
    lab.insert_events(second)
    wait_until(lambda: lab.counts().outbox_published == n1 + n2, 60, what="e second published")
    before_loss = lab.counts()
    lab.admin_redis.flushall()  # redis-events 유실
    after_loss = lab.counts()

    lab.start(idx)
    rc_status, status_out = lab.run_recovery("status")
    rc_dry, dry_out = lab.run_recovery(
        "replay", "--mode", "unreceipted", "--published-before-seconds", "0", "--dry-run"
    )
    rc_apply, apply_out = lab.run_recovery("replay", "--mode", "unreceipted", "--published-before-seconds", "0")
    assert (rc_status, rc_dry, rc_apply) == (0, 0, 0)
    wait_until(converged(lab, n1 + n2), 60, what="e replay converge")
    after_replay = lab.counts()
    assert_exact(lab, first + second)

    # 실행 중인 indexer가 NOGROUP을 만나도 group을 다시 만들고 계속 처리하는지
    lab.admin_redis.flushall()
    lab.insert_events(third)
    wait_until(converged(lab, n1 + n2 + n3), 60, what="e live nogroup")
    final = lab.counts()
    assert_exact(lab, first + second + third)
    lab.record(
        "e_redis_loss_replay",
        {
            "lost_in_redis": n2,
            "published_without_receipt": n2,
            "dry_run_would_reset": n2,
            "reset": n2,
            "es_docs_after_replay": n1 + n2,
            "es_docs_after_live_nogroup": n1 + n2 + n3,
            "pending": 0,
        },
        {
            "stream_len_before_loss": before_loss.stream_len,
            "stream_len_after_loss": after_loss.stream_len,
            "es_docs_before_replay": after_loss.es_docs,
            "published_without_receipt": _recovery_value(status_out, "published_without_receipt"),
            "dry_run_would_reset": _recovery_value(dry_out, "would_reset"),
            "reset": _recovery_value(apply_out, "reset"),
            "es_docs_after_replay": after_replay.es_docs,
            "receipts_after_replay": after_replay.receipts,
            "es_docs_after_live_nogroup": final.es_docs,
            "receipts_final": final.receipts,
            "pending": final.pending,
        },
        note="FLUSHALL → python -m pipeline.recovery replay --mode unreceipted → relay 재발행",
    )
    assert after_loss.stream_len == 0 and after_loss.es_docs == n1
    assert _recovery_value(dry_out, "would_reset") == n2
    assert _recovery_value(apply_out, "reset") == n2
    assert after_replay.es_docs == n1 + n2 and after_replay.receipts == n1 + n2
    assert final.es_docs == n1 + n2 + n3 and final.receipts == n1 + n2 + n3 and final.pending == 0


# ---------------------------------------------------------------- (f) 늦은 이벤트


def test_f_late_event_goes_to_occurred_at_index(fresh: Lab):
    lab = fresh
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_late = midnight - timedelta(microseconds=1)  # 어제 23:59:59.999999Z
    docs = [make_event(yesterday_late), make_event(midnight), make_event(now)]
    lab.run_relay("relay-1")
    lab.run_indexer("indexer-1")
    lab.insert_events(docs)
    wait_until(converged(lab, 3), 60, what="f converge")
    placed = {h["_id"]: h["_index"] for h in lab.es_docs()}
    receipt_index = dict(lab.sql("SELECT event_id, es_index FROM security_event_receipt"))
    expected = {
        docs[0]["event_id"]: index_for(yesterday_late),
        docs[1]["event_id"]: index_for(midnight),
        docs[2]["event_id"]: index_for(now),
    }
    lab.record(
        "f_late_event_index",
        {
            "occurred_at": [format_utc(yesterday_late), format_utc(midnight), format_utc(now)],
            "index": list(expected.values()),
        },
        {
            "index": [placed[d["event_id"]] for d in docs],
            "receipt_es_index": [receipt_index[d["event_id"]] for d in docs],
        },
        note="index 날짜는 처리 시각이 아니라 UTC occurred_at",
    )
    assert placed == expected
    assert receipt_index == expected


# ---------------------------------------------------------------- (g) poison


def test_g_poison_to_dlq_then_ack(fresh: Lab):
    lab = fresh
    good = events(5)
    raw_jwt = invalid_fixture("raw_jwt_in_field.json")
    not_utc = invalid_fixture("occurred_at_not_utc.json")
    poison = []
    for doc in (raw_jwt, not_utc):
        doc = dict(doc)
        doc["event_id"] = new_id()
        poison.append(doc)
    now = datetime.now(timezone.utc)
    lab.insert_events(good)
    lab.insert_events(poison, occurred_at=[now, now])
    lab.run_relay("relay-1")
    lab.run_indexer("indexer-1")
    wait_until(lambda: converged(lab, 5)() and lab.counts().dlq_len == 2, 60, what="g converge")
    c = lab.counts()
    dlq = [fields for _sid, fields in lab.admin_redis.xrange(DLQ)]
    rc, status_out = lab.run_recovery("status")
    lab.record(
        "g_poison_dlq",
        {
            "dlq_len": 2,
            "dlq_fields": ["event_id", "rule"],
            "es_docs": 5,
            "receipts": 5,
            "pending": 0,
            "published_without_receipt": 2,
        },
        {
            "dlq_len": c.dlq_len,
            "dlq_fields": sorted({k for f in dlq for k in f}),
            "dlq_rules": sorted(f["rule"] for f in dlq),
            "es_docs": c.es_docs,
            "receipts": c.receipts,
            "pending": c.pending,
            "published_without_receipt": _recovery_value(status_out, "published_without_receipt"),
        },
        note="payload 원문은 DLQ에 없고 MySQL Outbox에 남는다(receipt 없는 PUBLISHED 행으로 추적)",
    )
    assert c.dlq_len == 2 and c.es_docs == 5 and c.receipts == 5 and c.pending == 0
    assert all(set(f) == {"event_id", "rule"} for f in dlq)
    assert {f["event_id"] for f in dlq} == {d["event_id"] for d in poison}
    assert all("eyJ" not in v for f in dlq for v in f.values())
    assert rc == 0 and _recovery_value(status_out, "published_without_receipt") == 2


# ---------------------------------------------------------------- (i) XADD 중 DB lock 없음


def test_i_no_db_lock_held_while_xadd_is_blocked(fresh: Lab):
    lab = fresh
    n = 10
    docs = events(n)
    lab.insert_events(docs)
    lab.pause(lab.redis_container)  # XADD가 응답을 못 받는 상태(네트워크 대기)
    try:
        lab.run_relay("relay-1", RELAY_LEASE_SECONDS="5", REDIS_EVENTS_SOCKET_TIMEOUT_S="8", RELAY_BATCH_SIZE="50")
        wait_until(
            lambda: lab.sql("SELECT COUNT(*) FROM security_event_outbox WHERE lease_owner IS NOT NULL")[0][0] == n,
            30,
            what="lease committed",
        )
        time_in_xadd = lab.sql("SELECT COUNT(*) FROM security_event_outbox WHERE status='PENDING'")[0][0]
        # relay가 XADD 응답을 기다리는 동안 다른 세션이 같은 행을 NOWAIT로 잠글 수 있어야 한다.
        lab.mysql.begin()
        try:
            with lab.mysql.cursor() as cur:
                locked = cur.execute("SELECT id FROM security_event_outbox FOR UPDATE NOWAIT")
        finally:
            lab.mysql.rollback()
    finally:
        lab.unpause(lab.redis_container)
    lab.run_indexer("indexer-1")
    wait_until(lambda: converged(lab, n)() and lab.counts().outbox_published == n, 90, what="i converge")
    c = lab.counts()
    assert_exact(lab, docs)
    lab.record(
        "i_no_db_lock_during_xadd",
        {"rows_leased_while_redis_paused": n, "rows_lockable_nowait": n, "es_docs": n, "receipts": n},
        {
            "rows_leased_while_redis_paused": n,
            "pending_rows_during_xadd": int(time_in_xadd),
            "rows_lockable_nowait": int(locked),
            "es_docs": c.es_docs,
            "receipts": c.receipts,
            "stream_len": c.stream_len,
        },
        note="redis-events를 docker pause → relay는 lease commit 후 XADD 응답 대기. 그동안 FOR UPDATE NOWAIT 성공",
    )
    assert locked == n
    assert c.es_docs == n and c.receipts == n and c.pending == 0


# ---------------------------------------------------------------- 최소 권한


def _connect_as(lab: Lab, user: str, secret: str):
    host, port = lab.mysql.host, lab.mysql.port
    return pymysql.connect(
        host=host, port=port, user=user, password=lab.secret(secret), database="zeti_db", autocommit=True
    )


def _denied(conn, statement: str) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
        return False
    except pymysql.err.OperationalError as exc:
        return exc.args[0] in (1142, 1143, 1227)
    except pymysql.err.ProgrammingError as exc:
        return exc.args[0] in (1142, 1143, 1227)


def test_h_least_privilege_accounts(fresh: Lab):
    lab = fresh
    lab.insert_events(events(1))
    relay = _connect_as(lab, "zetty_relay", "mysql_relay")
    indexer = _connect_as(lab, "zetty_indexer", "mysql_indexer")
    checks = {
        "relay_update_payload_denied": _denied(relay, "UPDATE security_event_outbox SET payload = JSON_OBJECT()"),
        "relay_insert_outbox_denied": _denied(
            relay,
            "INSERT INTO security_event_outbox (event_id, producer, event_type, occurred_at, payload) "
            "VALUES ('x', 'api', 'X', NOW(), JSON_OBJECT())",
        ),
        "relay_read_receipt_denied": _denied(relay, "SELECT * FROM security_event_receipt"),
        "indexer_read_outbox_denied": _denied(indexer, "SELECT * FROM security_event_outbox"),
        "indexer_delete_receipt_denied": _denied(indexer, "DELETE FROM security_event_receipt"),
    }
    relay.close()
    indexer.close()

    host, port = (
        lab.admin_redis.connection_pool.connection_kwargs["host"],
        lab.admin_redis.connection_pool.connection_kwargs["port"],
    )
    r_relay = redis.Redis(host=host, port=port, username="zetty-relay", password=lab.secret("redis_relay"))
    r_indexer = redis.Redis(host=host, port=port, username="zetty-indexer", password=lab.secret("redis_indexer"))

    def redis_denied(fn) -> bool:
        try:
            fn()
            return False
        except redis.exceptions.NoPermissionError:
            return True

    checks.update(
        {
            "redis_relay_flushall_denied": redis_denied(r_relay.flushall),
            "redis_relay_read_denied": redis_denied(lambda: r_relay.xrange(STREAM)),
            "redis_indexer_xadd_main_stream_denied": redis_denied(lambda: r_indexer.xadd(STREAM, {"a": "b"})),
            "redis_indexer_other_key_denied": redis_denied(lambda: r_indexer.get("session:any")),
        }
    )
    lab.record("h_least_privilege", {k: True for k in checks}, checks)
    assert all(checks.values()), checks
