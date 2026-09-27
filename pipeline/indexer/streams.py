"""Redis Streams consumer group 조작."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import redis

from pipeline.indexer.contract import Rejected

Entry = tuple[str, "dict[str, Any] | None"]


@dataclass(frozen=True)
class ClaimPage:
    next_start: str
    entries: list[Entry]
    deleted_ids: list[str]


class Consumer(Protocol):
    def ensure_group(self) -> None: ...

    def read_new(self, count: int, block_ms: int) -> list[Entry]: ...

    def read_own_pending(self, start: str, count: int) -> list[Entry]: ...

    def autoclaim(self, min_idle_ms: int, start: str, count: int) -> ClaimPage: ...

    def ack(self, stream_ids: Sequence[str]) -> int: ...

    def dead_letter(self, rejected: Sequence[Rejected]) -> None: ...

    def pending_count(self) -> int: ...


def is_nogroup(exc: BaseException) -> bool:
    return isinstance(exc, redis.ResponseError) and str(exc).startswith("NOGROUP")


class RedisStreamConsumer:
    def __init__(self, client: redis.Redis, *, stream: str, group: str, consumer: str, dlq_stream: str) -> None:
        self._r = client
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.dlq_stream = dlq_stream

    def ensure_group(self) -> None:
        """group이 없으면 id 0부터 만든다. Redis 유실 뒤 relay가 먼저 XADD한 항목도 읽기 위해서다."""
        try:
            self._r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if not str(exc).startswith("BUSYGROUP"):
                raise

    def read_new(self, count: int, block_ms: int) -> list[Entry]:
        resp = self._r.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=count, block=block_ms)
        return _flatten(resp)

    def read_own_pending(self, start: str, count: int) -> list[Entry]:
        """이 consumer 이름으로 전달됐지만 ACK되지 않은 항목(재기동 전 처리 중이던 것)."""
        resp = self._r.xreadgroup(self.group, self.consumer, {self.stream: start}, count=count)
        return _flatten(resp)

    def autoclaim(self, min_idle_ms: int, start: str, count: int) -> ClaimPage:
        resp = self._r.xautoclaim(self.stream, self.group, self.consumer, min_idle_ms, start_id=start, count=count)
        next_start = resp[0]
        entries = [(sid, fields) for sid, fields in resp[1] if sid is not None]
        deleted = list(resp[2]) if len(resp) > 2 and resp[2] else []
        return ClaimPage(next_start=next_start, entries=entries, deleted_ids=deleted)

    def ack(self, stream_ids: Sequence[str]) -> int:
        if not stream_ids:
            return 0
        return int(self._r.xack(self.stream, self.group, *stream_ids))

    def dead_letter(self, rejected: Sequence[Rejected]) -> None:
        """격리 stream에는 event_id와 규칙 ID만 남긴다(payload 원문은 MySQL Outbox에 남아 있다)."""
        if not rejected:
            return
        pipe = self._r.pipeline(transaction=False)
        for item in rejected:
            pipe.xadd(self.dlq_stream, {"event_id": item.event_id, "rule": item.rule})
        pipe.execute()  # 하나라도 실패하면 예외 → 해당 항목은 ACK하지 않는다

    def pending_count(self) -> int:
        info = self._r.xpending(self.stream, self.group)
        return int(info.get("pending", 0)) if isinstance(info, dict) else 0


def _flatten(resp: Any) -> list[Entry]:
    entries: list[Entry] = []
    if not resp:
        return entries
    if isinstance(resp, dict):  # RESP3 형식
        streams = resp.items()
    else:
        streams = resp
    for _stream, items in streams:
        for item in items:
            sid, fields = item[0], item[1] if len(item) > 1 else None
            if sid is not None:
                entries.append((sid, fields))
    return entries
