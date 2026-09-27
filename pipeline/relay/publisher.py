"""Redis Stream 발행. 항목별 XADD 결과를 돌려주어 성공한 행만 PUBLISHED로 표시하게 한다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from pipeline.relay.store import OutboxRow


@dataclass(frozen=True)
class PublishResult:
    succeeded_ids: list[int]
    failed: int


class Publisher(Protocol):
    def publish(self, rows: Sequence[OutboxRow]) -> PublishResult: ...


class RedisStreamPublisher:
    def __init__(self, client, stream_key: str) -> None:
        self._client = client
        self._stream = stream_key

    def publish(self, rows: Sequence[OutboxRow]) -> PublishResult:
        if not rows:
            return PublishResult([], 0)
        pipe = self._client.pipeline(transaction=False)
        for row in rows:
            pipe.xadd(self._stream, {"event_id": row.event_id, "payload": row.payload})
        # 연결 오류는 예외로 올라가고 어떤 행도 표시하지 않는다(lease 만료 후 재발행, event_id로 멱등).
        results = pipe.execute(raise_on_error=False)
        succeeded = [row.id for row, res in zip(rows, results, strict=True) if not isinstance(res, Exception)]
        return PublishResult(succeeded, len(rows) - len(succeeded))
