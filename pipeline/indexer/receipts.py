"""적재 receipt 기록. 같은 event_id는 한 번만 남는다(`INSERT IGNORE`, indexed_at은 최초 값 유지)."""

from __future__ import annotations

from typing import Protocol, Sequence

from pipeline.common.connections import LazyMySQL

RECEIPT_INSERT_PREFIX = "INSERT IGNORE INTO security_event_receipt (event_id, es_index, indexed_at) VALUES "
RECEIPT_ROW = "(%s, %s, UTC_TIMESTAMP(6))"


class ReceiptStore(Protocol):
    def record(self, receipts: Sequence[tuple[str, str]]) -> int:
        """(event_id, es_index) 목록을 기록하고 새로 들어간 행 수를 반환한다. 실패하면 예외."""
        ...


class MySQLReceiptStore:
    def __init__(self, db: LazyMySQL) -> None:
        self._db = db

    def record(self, receipts: Sequence[tuple[str, str]]) -> int:
        if not receipts:
            return 0
        sql = RECEIPT_INSERT_PREFIX + ", ".join([RECEIPT_ROW] * len(receipts))
        params: list[str] = []
        for event_id, es_index in receipts:
            params.extend((event_id, es_index))
        conn = self._db.get()
        try:
            with conn.cursor() as cur:
                inserted = cur.execute(sql, params)
        except Exception:
            self._db.discard()
            raise
        return int(inserted)
