"""`python -m pipeline.indexer`

env:
  MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE, MYSQL_USER, MYSQL_PASSWORD | MYSQL_PASSWORD_FILE
  REDIS_EVENTS_HOST, REDIS_EVENTS_PORT, REDIS_EVENTS_DB, REDIS_EVENTS_USERNAME,
  REDIS_EVENTS_PASSWORD | REDIS_EVENTS_PASSWORD_FILE, EVENTS_STREAM_KEY, EVENTS_DLQ_STREAM_KEY
  ES_URL, ES_API_KEY | ES_API_KEY_FILE, ES_USERNAME + ES_PASSWORD | ES_PASSWORD_FILE, ES_CA_CERT,
  ES_TIMEOUT_S, ES_INDEX_PREFIX, ES_TEMPLATE_NAME, ES_TEMPLATE_PATH, ES_ENSURE_TEMPLATE
  INDEXER_GROUP, INDEXER_CONSUMER(기본 hostname), INDEXER_BATCH_SIZE, INDEXER_BLOCK_MS,
  INDEXER_CLAIM_IDLE_MS, INDEXER_CLAIM_INTERVAL_MS, INDEXER_MAX_PAYLOAD_BYTES, CONTRACTS_DIR
  METRICS_HOST, METRICS_PORT(기본 9102), HEALTH_STALE_AFTER_S, LOG_LEVEL
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

from prometheus_client import CollectorRegistry

from pipeline.common.config import (
    ConfigError,
    MetricsSettings,
    MySQLSettings,
    RedisSettings,
    default_consumer_name,
    env_bool,
    env_int,
    env_secret,
    env_str,
)
from pipeline.common.connections import LazyMySQL, connect_redis
from pipeline.common.runtime import Backoff, Health, install_stop_handlers, setup_logging, start_http_server
from pipeline.indexer.contract import (
    DEFAULT_INDEX_PREFIX,
    EventClassifier,
    default_contracts_dir,
    load_validator,
    verify_contract_snapshot,
)
from pipeline.indexer.es import BulkRequestError, ElasticsearchClient
from pipeline.indexer.indexer import Indexer, IndexerMetrics, IndexerSettings
from pipeline.indexer.receipts import MySQLReceiptStore
from pipeline.indexer.streams import RedisStreamConsumer

LOG = logging.getLogger("pipeline.indexer")

DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "es" / "security-events-v2-template.json"


def ensure_template(
    es: ElasticsearchClient, name: str, path: Path, install: bool, stop: threading.Event, health: Health
) -> bool:
    """template 없이 쓰면 dynamic mapping으로 index가 생겨 이후 strict 문서와 충돌한다. 준비될 때까지 기다린다."""
    body = json.loads(path.read_text(encoding="utf-8"))
    backoff = Backoff(initial_s=1.0, max_s=15.0)
    while not stop.is_set():
        health.beat()
        try:
            if install:
                es.put_index_template(name, body)
                LOG.info("index template installed name=%s", name)
                return True
            if es.index_template_exists(name):
                return True
            LOG.warning("index template missing name=%s; waiting", name)
        except BulkRequestError as exc:
            LOG.warning("index template check failed error=%s", exc)
        stop.wait(backoff.next())
    return False


def main() -> int:
    setup_logging()
    try:
        mysql_settings = MySQLSettings.from_env()
        redis_settings = RedisSettings.from_env()
        metrics_settings = MetricsSettings.from_env(default_port=9102)
        block_ms = env_int("INDEXER_BLOCK_MS", 2000, minimum=10, maximum=60000)
        settings = IndexerSettings(
            batch_size=env_int("INDEXER_BATCH_SIZE", 100, minimum=1, maximum=1000),
            block_ms=block_ms,
            claim_idle_ms=env_int("INDEXER_CLAIM_IDLE_MS", 60000, minimum=100),
            claim_interval_s=env_int("INDEXER_CLAIM_INTERVAL_MS", 15000, minimum=100) / 1000.0,
        )
        group = env_str("INDEXER_GROUP", "indexer")
        consumer_name = env_str("INDEXER_CONSUMER") or default_consumer_name()
        es_url = env_str("ES_URL", required=True)
        es = ElasticsearchClient(
            es_url,  # type: ignore[arg-type]
            api_key=env_secret("ES_API_KEY"),
            username=env_str("ES_USERNAME"),
            password=env_secret("ES_PASSWORD"),
            ca_cert=env_str("ES_CA_CERT"),
            timeout_s=float(env_int("ES_TIMEOUT_S", 30, minimum=1)),
        )
        index_prefix = env_str("ES_INDEX_PREFIX", DEFAULT_INDEX_PREFIX)
        template_name = env_str("ES_TEMPLATE_NAME", "security-events-v2")
        template_path = Path(env_str("ES_TEMPLATE_PATH") or DEFAULT_TEMPLATE_PATH)
        install_template = env_bool("ES_ENSURE_TEMPLATE", True)
        max_payload = env_int("INDEXER_MAX_PAYLOAD_BYTES", 65536, minimum=1024)
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2

    contracts_dir = default_contracts_dir()
    info = verify_contract_snapshot(contracts_dir)
    classifier = EventClassifier(
        load_validator(contracts_dir), index_prefix=index_prefix, max_payload_bytes=max_payload
    )
    LOG.info("contract revision=%s schema_sha256=%s", info.revision, info.schema_sha256)

    registry = CollectorRegistry()
    metrics = IndexerMetrics(registry)
    health = Health(stale_after_s=max(metrics_settings.stale_after_s, 3 * block_ms / 1000.0))
    start_http_server(metrics_settings.host, metrics_settings.port, registry, health)
    stop = install_stop_handlers()

    if not ensure_template(es, template_name, template_path, install_template, stop, health):  # type: ignore[arg-type]
        return 0

    redis_client = connect_redis(redis_settings, min_socket_timeout_s=block_ms / 1000.0 + 5)
    consumer = RedisStreamConsumer(
        redis_client,
        stream=redis_settings.stream_key,
        group=group,  # type: ignore[arg-type]
        consumer=consumer_name,
        dlq_stream=redis_settings.dlq_stream_key,
    )
    indexer = Indexer(
        consumer,
        classifier,
        es,
        MySQLReceiptStore(LazyMySQL(mysql_settings)),
        settings=settings,
        metrics=metrics,
        health=health,
    )
    LOG.info("indexer consumer=%s group=%s stream=%s", consumer_name, group, redis_settings.stream_key)
    indexer.run_forever(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
