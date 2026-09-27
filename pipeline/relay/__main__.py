"""`python -m pipeline.relay`

env:
  MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE, MYSQL_USER, MYSQL_PASSWORD | MYSQL_PASSWORD_FILE
  REDIS_EVENTS_HOST, REDIS_EVENTS_PORT, REDIS_EVENTS_DB, REDIS_EVENTS_USERNAME,
  REDIS_EVENTS_PASSWORD | REDIS_EVENTS_PASSWORD_FILE, EVENTS_STREAM_KEY
  RELAY_BATCH_SIZE, RELAY_POLL_INTERVAL_MS, RELAY_LEASE_SECONDS, RELAY_STATS_INTERVAL_MS, RELAY_OWNER
  METRICS_HOST, METRICS_PORT(기본 9101), HEALTH_STALE_AFTER_S, LOG_LEVEL
"""

from __future__ import annotations

import logging
import sys

from prometheus_client import CollectorRegistry

from pipeline.common.config import (
    ConfigError,
    MetricsSettings,
    MySQLSettings,
    RedisSettings,
    default_consumer_name,
    env_int,
    env_str,
)
from pipeline.common.connections import LazyMySQL, connect_redis
from pipeline.common.runtime import Health, install_stop_handlers, setup_logging, start_http_server
from pipeline.relay.publisher import RedisStreamPublisher
from pipeline.relay.relay import Relay, RelayMetrics, RelaySettings, make_owner_id
from pipeline.relay.store import MySQLOutboxStore

LOG = logging.getLogger("pipeline.relay")


def main() -> int:
    setup_logging()
    try:
        mysql_settings = MySQLSettings.from_env()
        redis_settings = RedisSettings.from_env()
        metrics_settings = MetricsSettings.from_env(default_port=9101)
        lease_seconds = env_int("RELAY_LEASE_SECONDS", 30, minimum=1, maximum=3600)
        settings = RelaySettings(
            batch_size=env_int("RELAY_BATCH_SIZE", 100, minimum=1, maximum=1000),
            poll_interval_s=env_int("RELAY_POLL_INTERVAL_MS", 1000, minimum=10) / 1000.0,
            lease_seconds=lease_seconds,
            stats_interval_s=env_int("RELAY_STATS_INTERVAL_MS", 5000, minimum=100) / 1000.0,
        )
        owner = env_str("RELAY_OWNER") or make_owner_id(default_consumer_name())
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2
    if len(owner) > 64:
        LOG.error("configuration error: RELAY_OWNER must be <= 64 chars")
        return 2

    registry = CollectorRegistry()
    metrics = RelayMetrics(registry)
    health = Health(stale_after_s=max(metrics_settings.stale_after_s, 3 * settings.poll_interval_s))
    start_http_server(metrics_settings.host, metrics_settings.port, registry, health)

    stop = install_stop_handlers()
    relay = Relay(
        MySQLOutboxStore(LazyMySQL(mysql_settings)),
        RedisStreamPublisher(connect_redis(redis_settings), redis_settings.stream_key),
        owner=owner,
        settings=settings,
        metrics=metrics,
        health=health,
    )
    LOG.info("relay owner=%s stream=%s", owner, redis_settings.stream_key)
    relay.run_forever(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
