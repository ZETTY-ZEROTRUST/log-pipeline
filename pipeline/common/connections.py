"""MySQL·Redis 연결 생성. 자격 증명은 settings에서만 읽고 로그에 남기지 않는다."""

from __future__ import annotations

import time
from typing import Any

import pymysql
import redis

from pipeline.common.config import MySQLSettings, RedisSettings

# 세션 시간대를 UTC로 고정하고 READ COMMITTED로 locking read의 gap lock을 피한다.
# (REPEATABLE READ의 next-key lock은 producer의 새 Outbox INSERT를 막을 수 있다.)
MYSQL_SESSION_INIT = (
    "SET SESSION time_zone = '+00:00', "
    "SESSION transaction_isolation = 'READ-COMMITTED', "
    "SESSION innodb_lock_wait_timeout = 5"
)


def connect_mysql(settings: MySQLSettings) -> pymysql.connections.Connection:
    kwargs: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "user": settings.user,
        "password": settings.password,
        "database": settings.database,
        "charset": "utf8mb4",
        "autocommit": True,
        "connect_timeout": settings.connect_timeout_s,
        "read_timeout": settings.read_timeout_s,
        "write_timeout": settings.write_timeout_s,
        "init_command": MYSQL_SESSION_INIT,
    }
    if settings.ssl_ca:
        kwargs["ssl"] = {"ca": settings.ssl_ca}
    return pymysql.connect(**kwargs)


def connect_redis(settings: RedisSettings, *, min_socket_timeout_s: float = 0.0) -> redis.Redis:
    """decode_responses=True: stream 필드(event_id, payload)를 str로 다룬다."""
    return redis.Redis(
        host=settings.host,
        port=settings.port,
        db=settings.db,
        username=settings.username,
        password=settings.password,
        socket_timeout=max(float(settings.socket_timeout_s), min_socket_timeout_s),
        socket_connect_timeout=5,
        decode_responses=True,
        health_check_interval=30,
    )


class LazyMySQL:
    """필요할 때 연결하고, 연결 오류가 나면 버려서 다음 호출에서 다시 연결한다.

    오래 쉬었던 연결은 서버 wait_timeout으로 끊겼을 수 있어 사용 전에 ping한다(재연결 시 init_command 재실행).
    """

    def __init__(
        self, settings: MySQLSettings, connect=connect_mysql, idle_ping_s: float = 60.0, clock=time.monotonic
    ) -> None:
        self._settings = settings
        self._connect = connect
        self._conn: Any = None
        self._idle_ping_s = idle_ping_s
        self._clock = clock
        self._last_used = 0.0

    def get(self) -> Any:
        now = self._clock()
        if self._conn is None:
            self._conn = self._connect(self._settings)
        elif now - self._last_used > self._idle_ping_s:
            self._conn.ping(reconnect=True)
        self._last_used = now
        return self._conn

    def discard(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 이미 끊긴 연결 정리
                pass
