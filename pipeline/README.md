# pipeline — Outbox relay → Redis Streams → Elasticsearch indexer

SecurityEvent v2(C-02) 이벤트를 **업무 DB transaction과 함께 기록된 Outbox에서 ES까지** 옮기는 전달 경로다.
보장은 **at-least-once 전달 + 멱등 적재**다. exactly-once 전달을 주장하지 않는다.

- 공유 계약: `zetty-wt/shared/outbox-contract.md` (테이블·stream·group·DLQ·index 이름)
- 이벤트 계약: `contracts/security-event/v2/schema.json`, 검증기 `contracts/tools/validate.py` (indexer가 그대로 불러 쓴다)
- 설계·결과 기록: [`docs/star/P-02-outbox-relay-indexer.md`](../docs/star/P-02-outbox-relay-indexer.md)

## 구조

```text
producer(auth/api)  ──(업무와 같은 DB transaction)──▶  MySQL security_event_outbox (status=PENDING)
                                                         │
outbox-relay  ① BEGIN; SELECT … FOR UPDATE SKIP LOCKED; lease 기록; COMMIT   (READ COMMITTED, 짧은 transaction)
              ② XADD zetty:security-events {event_id, payload}              (DB lock 없음)
              ③ UPDATE … PUBLISHED WHERE lease_owner = 나                     (lease 잃은 행은 건드리지 않음)
                                                         │
redis-events  stream zetty:security-events, consumer group indexer
                                                         │
event-indexer ① XREADGROUP (기동 시 자기 PEL 먼저, 주기적 XAUTOCLAIM)
              ② C-02 검증 ── 위반 ──▶ XADD zetty:security-events:dlq {event_id, rule} → XACK
              ③ ES _bulk  index, _id=event_id, index=security-events-v2-<UTC occurred_at 날짜>
              ④ 항목별 status 확인 → 성공 항목만 INSERT IGNORE security_event_receipt → 그 항목만 XACK
                 실패 항목은 pending으로 남아 XAUTOCLAIM으로 재시도
```

| 경로 | 책임 |
|---|---|
| `relay/` | lease 획득(`store.py`), XADD(`publisher.py`), 루프·지표(`relay.py`) |
| `indexer/` | C-02 분류(`contract.py`), ES bulk 항목 해석(`es.py`), receipt(`receipts.py`), stream 조작(`streams.py`), 루프(`indexer.py`) |
| `recovery/` | Outbox replay 운영 명령 |
| `es/security-events-v2-template.json` | C-02 필드와 1:1인 strict index template |
| `sql/least-privilege-grants.sql` | DB 계정 최소 권한 |
| `redis-events/acl-rules.txt` | redis-events ACL 규칙 |
| `tests/unit`, `tests/integration` | fake 기반 unit test, 일회용 컨테이너 복구 시나리오 |

### 중복·유실을 막는 장치

| 위험 | 장치 |
|---|---|
| relay가 XADD 후 표시 전에 종료 | lease 만료 뒤 다른 relay가 재발행 → 같은 event_id → ES `_id` 덮어쓰기, receipt `INSERT IGNORE` |
| 여러 relay가 같은 행을 동시에 발행 | `FOR UPDATE SKIP LOCKED` + lease(`lease_until`), 표시는 `lease_owner = 나` 조건 |
| relay가 네트워크 대기 중 DB lock 보유 | lease commit 뒤에 XADD. READ COMMITTED로 gap lock 없음 |
| indexer가 ES 기록 후 XACK 전에 종료 | 재기동 시 자기 PEL(`XREADGROUP … 0`) 또는 다른 consumer의 `XAUTOCLAIM` → 같은 `_id` 덮어쓰기 |
| bulk 일부 실패 | HTTP 200만 보지 않고 항목별 status·`_id`·순서 확인. 실패 항목은 ACK하지 않음 |
| Redis 유실 | Outbox가 원본. `recovery replay`로 receipt 없는 행을 PENDING으로 되돌려 재발행. indexer는 `NOGROUP`이면 group을 `0`부터 재생성 |
| 늦은 이벤트 | index 날짜는 처리 시각이 아니라 UTC `occurred_at` |
| schema 위반(poison) | DLQ에 event_id·규칙 ID만(값 없음) → ACK. 원문은 MySQL Outbox에 남음 |

## 환경 변수

비밀 값은 `NAME` 또는 `NAME_FILE`(secret 파일 경로) 중 하나만 준다. 둘 다 주면 기동하지 않는다. 설정 오류 메시지·로그에는 변수 이름만 나오고 값은 나오지 않는다.

### 공통

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MYSQL_HOST` | (필수) | |
| `MYSQL_PORT` | `3306` | |
| `MYSQL_DATABASE` | `zeti_db` | |
| `MYSQL_USER` | (필수) | relay=`zetty_relay`, indexer=`zetty_indexer`, recovery=`zetty_pipeline_ops` |
| `MYSQL_PASSWORD` / `MYSQL_PASSWORD_FILE` | (필수) | |
| `MYSQL_SSL_CA` | 없음 | CA 경로를 주면 TLS 사용 |
| `MYSQL_CONNECT_TIMEOUT_S` / `MYSQL_READ_TIMEOUT_S` / `MYSQL_WRITE_TIMEOUT_S` | `5` / `30` / `30` | |
| `REDIS_EVENTS_HOST` | (필수) | 세션 Redis와 분리된 `redis-events` |
| `REDIS_EVENTS_PORT` / `REDIS_EVENTS_DB` | `6379` / `0` | |
| `REDIS_EVENTS_USERNAME` | 없음 | ACL 사용자(`zetty-relay`, `zetty-indexer`) |
| `REDIS_EVENTS_PASSWORD` / `REDIS_EVENTS_PASSWORD_FILE` | 없음 | |
| `REDIS_EVENTS_SOCKET_TIMEOUT_S` | `10` | indexer는 `BLOCK + 5초` 이상으로 자동 상향 |
| `EVENTS_STREAM_KEY` / `EVENTS_DLQ_STREAM_KEY` | `zetty:security-events` / `zetty:security-events:dlq` | |
| `METRICS_HOST` / `METRICS_PORT` | `0.0.0.0` / relay `9101`, indexer `9102` | 포트를 바꾸면 이미지 HEALTHCHECK도 바꿔야 한다 |
| `HEALTH_STALE_AFTER_S` | `60` | `/healthz`(루프 heartbeat), `/readyz`(최근 성공) 기준 |
| `LOG_LEVEL` | `INFO` | |

### relay

| 변수 | 기본값 | 설명 |
|---|---|---|
| `RELAY_BATCH_SIZE` | `100` | 한 번에 lease로 잡는 행 수(1~1000) |
| `RELAY_POLL_INTERVAL_MS` | `1000` | 잡은 행이 batch보다 적을 때 다음 poll까지 대기 |
| `RELAY_LEASE_SECONDS` | `30` | lease 만료 후 다른 relay가 다시 잡는다. XADD 최대 대기보다 길게 둔다 |
| `RELAY_STATS_INTERVAL_MS` | `5000` | lag/pending 지표 갱신 주기 |
| `RELAY_OWNER` | `hostname:pid:난수` | lease_owner(64자 이하). 보통 지정하지 않는다 |

### indexer

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ES_URL` | (필수) | 예: `http://elasticsearch:9200` |
| `ES_API_KEY` / `ES_API_KEY_FILE` | 없음 | 또는 `ES_USERNAME` + `ES_PASSWORD`(`_FILE`) |
| `ES_CA_CERT` | 없음 | HTTPS 검증용 CA |
| `ES_TIMEOUT_S` | `30` | bulk 응답 대기 |
| `ES_INDEX_PREFIX` | `security-events-v2-` | 뒤에 `YYYY.MM.DD`(UTC occurred_at) |
| `ES_TEMPLATE_NAME` / `ES_TEMPLATE_PATH` | `security-events-v2` / 이미지 내 `pipeline/es/…json` | |
| `ES_ENSURE_TEMPLATE` | `true` | 기동 시 template PUT. `false`면 존재만 확인하고 없으면 기다린다 |
| `INDEXER_GROUP` | `indexer` | |
| `INDEXER_CONSUMER` | 컨테이너 hostname | 같은 이름으로 재기동하면 자기 pending부터 처리 |
| `INDEXER_BATCH_SIZE` | `100` | XREADGROUP COUNT |
| `INDEXER_BLOCK_MS` | `2000` | XREADGROUP BLOCK |
| `INDEXER_CLAIM_IDLE_MS` | `60000` | 이보다 오래 ACK되지 않은 항목을 XAUTOCLAIM |
| `INDEXER_CLAIM_INTERVAL_MS` | `15000` | XAUTOCLAIM·pending 지표 주기 |
| `INDEXER_MAX_PAYLOAD_BYTES` | `65536` | 초과 payload는 poison(`ENVELOPE_PAYLOAD_TOO_LARGE`) |
| `CONTRACTS_DIR` | 이미지 `/app/contracts` | 기동 시 schema sha256을 `MANIFEST.json`과 대조 |

`PIPELINE_FAILPOINT`(`relay_after_xadd`, `indexer_after_bulk`, `indexer_after_receipt`)는 **통합 시험 전용** 장애 주입이다. 운영에서 설정하지 않는다.

## 권한

DB(`sql/least-privilege-grants.sql`, 통합 시험이 그대로 적용해 확인):

| 계정 | 권한 |
|---|---|
| `zetty_relay` | outbox `SELECT` + `UPDATE(status, lease_owner, lease_until, attempts, published_at)` |
| `zetty_indexer` | receipt `SELECT, INSERT` |
| `zetty_pipeline_ops` | outbox `SELECT` + `UPDATE(status, lease_owner, lease_until)`, receipt `SELECT` (replay 전용) |

redis-events ACL(`redis-events/acl-rules.txt`): relay는 `XADD`(stream 키만), indexer는 `XREADGROUP/XACK/XAUTOCLAIM/XGROUP CREATE/XPENDING`(stream) + `XADD`(DLQ 키만).

## 지표

| 이름 | 종류 | 뜻 |
|---|---|---|
| `zetty_relay_published_total` | counter | XADD 성공 행(재발행 포함) |
| `zetty_relay_marked_published_total` | counter | PUBLISHED 표시 행 |
| `zetty_relay_publish_failures_total` / `zetty_relay_lease_lost_total` | counter | XADD 실패 / 표시 전에 lease를 잃은 행(중복 발행 가능) |
| `zetty_relay_attempts` | histogram | lease 획득 시 누적 attempts |
| `zetty_relay_lag_seconds` | gauge | now − 가장 오래된 PENDING 행의 occurred_at |
| `zetty_relay_pending_rows` | gauge | PENDING 행 수 |
| `zetty_indexer_indexed_total` | counter | ES 성공 + receipt + XACK 완료 |
| `zetty_indexer_failed_items_total{error_type}` | counter | bulk 항목 실패(ACK 안 함) |
| `zetty_indexer_bulk_request_errors_total` | counter | bulk 요청 전체 실패 |
| `zetty_indexer_dlq_total{layer}` | counter | poison 격리 |
| `zetty_indexer_pending` | gauge | consumer group pending |
| `zetty_indexer_reclaimed_total`, `zetty_indexer_receipts_inserted_total`, `zetty_indexer_bulk_seconds` | | 회수·신규 receipt·bulk 시간 |

`late 이벤트`도 `zetty_relay_lag_seconds`를 키운다(occurred_at 기준). 전달 지연만 보려면 `zetty_relay_pending_rows`와 함께 본다.

## 실행

```bash
# 이미지 (build context = 저장소 루트)
docker build -f pipeline/relay/Dockerfile   -t zetty-outbox-relay:dev .
docker build -f pipeline/indexer/Dockerfile -t zetty-event-indexer:dev .

# 로컬 시험 환경(Python 3.12)
python3.12 -m venv pipeline/.venv
pipeline/.venv/bin/pip install -r pipeline/requirements-test.txt

# unit test·lint
pipeline/.venv/bin/python -m pytest pipeline/tests/unit
pipeline/.venv/bin/python -m ruff check pipeline

# 통합·복구 시나리오(일회용 MySQL·Redis·ES 컨테이너를 띄우고 끝나면 모두 삭제)
PYTHON=pipeline/.venv/bin/python pipeline/tests/integration/run.sh
```

`run.sh`는 k6 부하 시험 컨테이너가 돌고 있으면 끝날 때까지 기다린다. ES는 heap 512m 한 개만 띄운다.

## 복구 runbook

replay 명령은 relay 이미지에 들어 있다. 운영 계정으로 실행한다.

```bash
docker run --rm --network <data 네트워크> \
  -e MYSQL_HOST=mysql -e MYSQL_USER=zetty_pipeline_ops -e MYSQL_PASSWORD_FILE=/run/secrets/mysql_pipeline_ops \
  -v <secret 경로>:/run/secrets:ro \
  --entrypoint python zetty-outbox-relay:<tag> -m pipeline.recovery <명령>
```

| 상황 | 절차 |
|---|---|
| **redis-events 유실**(재기동 후 빈 인스턴스, FLUSHALL, volume 손실) | 1) `status`로 `published_without_receipt` 확인 2) `replay --mode unreceipted --published-before-seconds 60 --dry-run`으로 건수 확인 3) `--dry-run` 없이 실행 4) relay가 재발행, indexer는 group을 자동 재생성 5) `status`의 `published_without_receipt`가 poison 건수까지 줄어드는지 확인 |
| **ES 데이터 유실·재구축** | template 확인(indexer 기동 시 설치) → `replay --mode all` → 모든 행 재발행, 문서는 `_id=event_id`로 덮어쓰기. receipt는 최초 기록 유지 |
| **ES 항목 실패가 계속됨** | `zetty_indexer_failed_items_total{error_type}`와 `zetty_indexer_pending` 확인. 닫힌 index·write block·mapping 문제를 해결하면 XAUTOCLAIM이 자동 재시도한다. pending을 수동으로 ACK하지 않는다 |
| **poison 확인** | `XRANGE zetty:security-events:dlq - +`(event_id, rule). DLQ가 유실돼도 `status`의 `published_without_receipt`와 Outbox 원문으로 다시 찾을 수 있다(검증은 결정적이라 재발행하면 같은 rule이 나온다) |
| **relay 적체** | `zetty_relay_pending_rows`·`zetty_relay_lag_seconds`·`zetty_relay_errors_total{stage}` 확인. 오래 lease가 걸린 행은 `lease_until`이 지나면 자동으로 다시 잡힌다 |

`--published-before-seconds`는 막 발행돼 아직 적재 중인 행을 되돌리지 않기 위한 여유다. 겹쳐도 event_id로 멱등이다.

## 알려진 한계

- **at-least-once + 멱등**이다. stream에는 중복 항목이 생길 수 있고(ES·receipt에는 중복 없음), exactly-once 전달이 아니다.
- 보장 경계는 Outbox commit 이후다. 단일 호스트에서 MySQL volume까지 잃는 장애, commit 전 producer 종료는 다루지 않는다.
- `index` action은 같은 event_id의 문서를 덮어쓴다. 서로 다른 내용이 같은 event_id로 오면(producer 버그) 마지막 내용이 남고 indexer는 충돌을 탐지하지 않는다.
- DLQ는 Redis stream이라 유실될 수 있다. 원문은 Outbox에 남지만 **사유 코드의 내구성 저장소(예: MySQL quarantine 표)는 아직 없다**(COMPOSE.md §6 요구와 차이, 계약 변경 필요).
- stream을 자동 trim하지 않는다. 메모리 상한·`noeviction`을 두고, 필요하면 pending 최소 ID 이전만 `XTRIM MINID`로 정리한다.
- consumer 이름이 hostname이라 컨테이너를 새로 만들면 오래된 consumer가 group에 남는다. pending 0인 consumer는 `XGROUP DELCONSUMER`로 정리한다.
- 순서: relay 여러 개·재발행 때문에 stream 순서는 발생 순서가 아니다. 소비자는 `occurred_at`으로 정렬한다.
- ES 보존(ILM)·index 삭제 정책은 구현하지 않았다. template의 `number_of_replicas: 0`은 single-node lab 기준이다.
- requirements는 버전 고정이며 hash 고정(`--require-hashes`)은 아니다.
- `/metrics`에는 인증이 없다. analysis 네트워크 안에서만 노출하고 host port로 publish하지 않는다.
