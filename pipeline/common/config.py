"""환경 변수 설정.

- 비밀 값은 `NAME` 또는 `NAME_FILE`(Docker secret mount 경로) 중 하나로 받는다.
- 오류 메시지에는 변수 이름만 넣고 값은 넣지 않는다.
- 비밀 필드는 dataclass repr에서 제외해 실수로 로그에 찍히지 않게 한다.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    """설정 누락·형식 오류. 메시지에 값은 들어가지 않는다."""


def env_str(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        if required:
            raise ConfigError("%s is required" % name)
        return default
    return value


def env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError("%s must be an integer" % name) from None
    if minimum is not None and value < minimum:
        raise ConfigError("%s must be >= %d" % (name, minimum))
    if maximum is not None and value > maximum:
        raise ConfigError("%s must be <= %d" % (name, maximum))
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError("%s must be a boolean" % name)


def env_secret(name: str, *, required: bool = False) -> str | None:
    """`NAME_FILE`이 있으면 파일 내용(끝 개행 제거), 없으면 `NAME` 값."""
    file_var = name + "_FILE"
    path = os.environ.get(file_var)
    direct = os.environ.get(name)
    if path:
        if direct:
            raise ConfigError("%s and %s are both set" % (name, file_var))
        try:
            return Path(path).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            raise ConfigError("%s is not readable" % file_var) from None
    if direct:
        return direct
    if required:
        raise ConfigError("%s (or %s) is required" % (name, file_var))
    return None


def default_consumer_name() -> str:
    """Compose에서는 컨테이너 호스트명(container id)이 기본 소비자 이름이 된다."""
    return socket.gethostname()


@dataclass(frozen=True)
class MySQLSettings:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    connect_timeout_s: int = 5
    read_timeout_s: int = 30
    write_timeout_s: int = 30
    ssl_ca: str | None = None

    @classmethod
    def from_env(cls) -> MySQLSettings:
        return cls(
            host=env_str("MYSQL_HOST", required=True),  # type: ignore[arg-type]
            port=env_int("MYSQL_PORT", 3306, minimum=1, maximum=65535),
            database=env_str("MYSQL_DATABASE", "zeti_db"),  # type: ignore[arg-type]
            user=env_str("MYSQL_USER", required=True),  # type: ignore[arg-type]
            password=env_secret("MYSQL_PASSWORD", required=True),  # type: ignore[arg-type]
            connect_timeout_s=env_int("MYSQL_CONNECT_TIMEOUT_S", 5, minimum=1),
            read_timeout_s=env_int("MYSQL_READ_TIMEOUT_S", 30, minimum=1),
            write_timeout_s=env_int("MYSQL_WRITE_TIMEOUT_S", 30, minimum=1),
            ssl_ca=env_str("MYSQL_SSL_CA"),
        )


@dataclass(frozen=True)
class RedisSettings:
    host: str
    port: int
    db: int
    username: str | None
    password: str | None = field(repr=False)
    socket_timeout_s: int = 10
    stream_key: str = "zetty:security-events"
    dlq_stream_key: str = "zetty:security-events:dlq"

    @classmethod
    def from_env(cls) -> RedisSettings:
        return cls(
            host=env_str("REDIS_EVENTS_HOST", required=True),  # type: ignore[arg-type]
            port=env_int("REDIS_EVENTS_PORT", 6379, minimum=1, maximum=65535),
            db=env_int("REDIS_EVENTS_DB", 0, minimum=0),
            username=env_str("REDIS_EVENTS_USERNAME"),
            password=env_secret("REDIS_EVENTS_PASSWORD"),
            socket_timeout_s=env_int("REDIS_EVENTS_SOCKET_TIMEOUT_S", 10, minimum=1),
            stream_key=env_str("EVENTS_STREAM_KEY", "zetty:security-events"),  # type: ignore[arg-type]
            dlq_stream_key=env_str("EVENTS_DLQ_STREAM_KEY", "zetty:security-events:dlq"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class MetricsSettings:
    host: str
    port: int
    stale_after_s: int

    @classmethod
    def from_env(cls, default_port: int) -> MetricsSettings:
        return cls(
            host=env_str("METRICS_HOST", "0.0.0.0"),  # type: ignore[arg-type]
            port=env_int("METRICS_PORT", default_port, minimum=0, maximum=65535),
            stale_after_s=env_int("HEALTH_STALE_AFTER_S", 60, minimum=1),
        )
