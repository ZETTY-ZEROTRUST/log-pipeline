"""env 설정: 비밀은 NAME 또는 NAME_FILE, 오류 메시지·repr에 값이 없다."""

from __future__ import annotations

import pytest

from pipeline.common.config import ConfigError, MySQLSettings, RedisSettings, env_int, env_secret
from pipeline.common.runtime import Health, error_label


def test_secret_from_file_strips_newline(tmp_path, monkeypatch):
    secret = tmp_path / "pw"
    secret.write_text("s3cr3t-value\n", encoding="utf-8")
    monkeypatch.setenv("X_PASSWORD_FILE", str(secret))
    monkeypatch.delenv("X_PASSWORD", raising=False)
    assert env_secret("X_PASSWORD") == "s3cr3t-value"


def test_secret_both_forms_is_error_without_value(tmp_path, monkeypatch):
    secret = tmp_path / "pw"
    secret.write_text("file-value", encoding="utf-8")
    monkeypatch.setenv("X_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("X_PASSWORD", "env-value")
    with pytest.raises(ConfigError) as err:
        env_secret("X_PASSWORD")
    assert "value" not in str(err.value).replace("X_PASSWORD", "")


def test_required_secret_missing(monkeypatch):
    monkeypatch.delenv("X_PASSWORD", raising=False)
    monkeypatch.delenv("X_PASSWORD_FILE", raising=False)
    with pytest.raises(ConfigError):
        env_secret("X_PASSWORD", required=True)


def test_settings_repr_hides_password(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "mysql")
    monkeypatch.setenv("MYSQL_USER", "zetty_relay")
    monkeypatch.setenv("MYSQL_PASSWORD", "hunter2-do-not-log")
    monkeypatch.setenv("REDIS_EVENTS_HOST", "redis-events")
    monkeypatch.setenv("REDIS_EVENTS_PASSWORD", "redis-do-not-log")
    mysql = MySQLSettings.from_env()
    redis = RedisSettings.from_env()
    assert mysql.password == "hunter2-do-not-log"
    assert "hunter2" not in repr(mysql)
    assert "redis-do-not-log" not in repr(redis)
    assert redis.stream_key == "zetty:security-events"
    assert redis.dlq_stream_key == "zetty:security-events:dlq"


def test_env_int_bounds(monkeypatch):
    monkeypatch.setenv("N", "0")
    with pytest.raises(ConfigError):
        env_int("N", 5, minimum=1)
    monkeypatch.setenv("N", "abc")
    with pytest.raises(ConfigError):
        env_int("N", 5)


def test_error_label_drops_message():
    exc = RuntimeError("password=hunter2 in message")
    assert error_label(exc) == "RuntimeError"
    assert error_label(OSError(1045, "Access denied for user 'x'")) == "OSError:1045"


def test_health_liveness_and_readiness():
    now = [0.0]
    health = Health(stale_after_s=10, clock=lambda: now[0])
    assert health.alive() and not health.ready()
    health.success()
    now[0] = 5
    assert health.ready()
    now[0] = 20
    assert not health.alive() and not health.ready()
    health.beat()
    assert health.alive() and not health.ready()


def test_lazy_mysql_pings_only_after_idle_and_reconnects_after_discard():
    from pipeline.common.connections import LazyMySQL

    class Conn:
        def __init__(self) -> None:
            self.pings = 0
            self.closed = False

        def ping(self, reconnect=False):
            assert reconnect is True
            self.pings += 1

        def close(self):
            self.closed = True

    made: list[Conn] = []
    now = [0.0]

    def connect(_settings):
        made.append(Conn())
        return made[-1]

    db = LazyMySQL(object(), connect=connect, idle_ping_s=60, clock=lambda: now[0])  # type: ignore[arg-type]
    first = db.get()
    now[0] = 30
    assert db.get() is first and first.pings == 0
    now[0] = 100
    assert db.get() is first and first.pings == 1
    db.discard()
    assert first.closed
    assert db.get() is not first and len(made) == 2
