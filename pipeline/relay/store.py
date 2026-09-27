"""Outbox 저장소 접근.

lease 획득은 짧은 transaction 하나로 끝내고 commit한 뒤에 반환한다. 호출자는 반환된 행을
네트워크로 보내는 동안 DB lock을 잡고 있지 않다. 시각은 DB의 UTC_TIMESTAMP(6) 하나만 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from pipeline.common.connections import LazyMySQL

CLAIM_SELECT_SQL = (
    "SELECT id, event_id, payload, attempts FROM security_event_outbox "
    "WHERE status = 'PENDING' AND (lease_until IS NULL OR lease_until < UTC_TIMESTAMP(6)) "
    "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED"
)

CLAIM_UPDATE_SQL = (
    "UPDATE security_event_outbox "
    "SET lease_owner = %s, lease_until = UTC_TIMESTAMP(6) + INTERVAL %s SECOND, attempts = attempts + 1 "
    "WHERE id IN ({ids})"
)

# lease를 잃은 행(다른 relay가 다시 잡음)은 바꾸지 않는다.
MARK_PUBLISHED_SQL = (
    "UPDATE security_event_outbox SET status = 'PUBLISHED', published_at = UTC_TIMESTAMP(6) "
    "WHERE id IN ({ids}) AND lease_owner = %s AND status = 'PENDING'"
)

PENDING_STATS_SQL = (
    "SELECT COUNT(*), TIMESTAMPDIFF(MICROSECOND, MIN(occurred_at), UTC_TIMESTAMP(6)) "
    "FROM security_event_outbox WHERE status = 'PENDING'"
)


@dataclass(frozen=True)
class OutboxRow:
    id: int
    event_id: str
    payload: str
    attempts: int  # 이번 lease로 증가한 뒤의 값


@dataclass(frozen=True)
class PendingStats:
    pending_rows: int
    lag_seconds: float  # now - 가장 오래된 PENDING 행의 occurred_at (없으면 0)


class OutboxStore(Protocol):
    def claim(self, owner: str, batch_size: int, lease_seconds: int) -> list[OutboxRow]: ...

    def mark_published(self, owner: str, ids: Sequence[int]) -> int: ...

    def pending_stats(self) -> PendingStats: ...


def _placeholders(count: int) -> str:
    return ", ".join(["%s"] * count)


class MySQLOutboxStore:
    def __init__(self, db: LazyMySQL) -> None:
        self._db = db

    def claim(self, owner: str, batch_size: int, lease_seconds: int) -> list[OutboxRow]:
        conn = self._db.get()
        try:
            conn.begin()
            with conn.cursor() as cur:
                cur.execute(CLAIM_SELECT_SQL, (batch_size,))
                rows = cur.fetchall()
                if rows:
                    ids = [int(r[0]) for r in rows]
                    cur.execute(
                        CLAIM_UPDATE_SQL.format(ids=_placeholders(len(ids))),
                        (owner, lease_seconds, *ids),
                    )
            conn.commit()
        except Exception:
            self._rollback_and_discard(conn)
            raise
        return [
            OutboxRow(id=int(r[0]), event_id=str(r[1]), payload=_as_text(r[2]), attempts=int(r[3]) + 1) for r in rows
        ]

    def mark_published(self, owner: str, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        conn = self._db.get()
        try:
            with conn.cursor() as cur:
                changed = cur.execute(MARK_PUBLISHED_SQL.format(ids=_placeholders(len(ids))), (*ids, owner))
        except Exception:
            self._db.discard()
            raise
        return int(changed)

    def pending_stats(self) -> PendingStats:
        conn = self._db.get()
        try:
            with conn.cursor() as cur:
                cur.execute(PENDING_STATS_SQL)
                count, lag_us = cur.fetchone()
        except Exception:
            self._db.discard()
            raise
        return PendingStats(pending_rows=int(count or 0), lag_seconds=max(0.0, float(lag_us or 0) / 1_000_000))

    def _rollback_and_discard(self, conn) -> None:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - 끊긴 연결이면 rollback도 실패한다
            pass
        self._db.discard()


def _as_text(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return str(value)
