# P-02 Outbox relay → Redis Streams → Elasticsearch indexer

- 상태: 계획
- 연결: Jira A-06 · I-02 · 공유 계약 `zetty-wt/shared/outbox-contract.md` · 명세 `docs/contracts.md` §6(owner 문서, main tree) · `zero-trust-architecture/docs/COMPOSE.md` §6 · 선행 [P-01](P-01-c02-contracts.md)
- 작성/갱신: 2026-09-27 (계획)

## S — 문제 발생 (Situation)

- SecurityEvent v2 계약(P-01)은 생겼지만 **이벤트를 안전하게 전달·적재할 경로가 없다.** producer(auth/api)가 Outbox에 기록한 이벤트를 Redis Streams를 거쳐 ES에 넣는 relay/indexer 코드가 저장소에 없다.
- 현재(v1) 경로는 Nginx access log tail → Filebeat → ES ingest pipeline이다.
  - `nginx-pep/uba.conf:34` 의 `log_format uba_log` 가 `"jwt":"$http_authorization"` 로 **원문 Authorization 헤더(Bearer JWT)를 로그 파일에 기록**하고, `filebeat/filebeat-priv-web.yml:49-65` 가 그 값을 읽어 분해한다. 즉 raw bearer token이 디스크·수집기·ES 경로를 모두 지난다.
  - 파일 tail은 업무 DB transaction과 묶이지 않는다. 업무는 commit됐는데 로그 행이 rotation·registry 손실로 빠지거나, 같은 행이 재수집돼 ES에 중복 문서가 생겨도 이를 판정할 안정된 ID가 없다(문서 ID가 자동 생성).
  - 적재 성공 여부를 원본 쪽에 되돌려 기록하는 receipt가 없어 "무엇이 유실됐는가"를 사후에 계산할 수 없다.
- `docs/contracts.md` §6과 `COMPOSE.md` §6은 Outbox·ACK 복구 규칙(항목별 성공 확인, ACK 전 종료 멱등, Redis 손실 시 Outbox 재발행)을 요구하지만, 이를 실행으로 확인한 결과가 없다.

## T — 왜 / 목표 (Task)

성공 기준(모두 일회용 컨테이너 MySQL 8.0 · Redis 7 · Elasticsearch 8.19 single-node에서 명령 실행 결과로 확인한다).

| # | 시나리오 | 기대 |
|---|---|---|
| a | 정상 경로 N건 | ES 문서 N, receipt N, Outbox PUBLISHED N, PEL 0 |
| b | 같은 event_id 중복 발행(relay가 XADD 후 PUBLISHED 표시 전 종료 → lease 만료 후 재발행) | stream 항목 > N 이어도 ES 문서 N, receipt N |
| c | indexer가 ES 기록 후 XACK 전에 종료 | 재기동/XAUTOCLAIM 뒤 ES 문서 N(중복 0), receipt N, PEL 0 |
| d | ES bulk 부분 실패(닫힌 index로 가는 항목) | 실패 항목만 ACK 안 됨(PEL = 실패 수), 나머지 ACK·receipt. 원인 해소 후 N으로 수렴 |
| e | Redis 유실(FLUSHALL) | 문서화한 복구 명령으로 Outbox 행을 PENDING으로 되돌리면 relay가 재발행해 ES N으로 수렴 |
| f | 늦은 이벤트(occurred_at 어제) | 어제 날짜 index(`security-events-v2-YYYY.MM.DD`)에 들어감 |
| g | schema 위반(poison) | DLQ에 event_id·규칙 ID만 기록, ACK, ES 문서·receipt 없음 |

공통 기준:

1. **중복 0·유실 0**: 수렴 후 `security-events-v2-*` 전체 문서 수 = 고유 event_id 수 N, 기대 event_id 집합과 ES `_id` 집합이 같다.
2. relay는 네트워크 I/O(XADD) 동안 DB lock을 잡지 않는다: lease 획득 transaction을 commit한 뒤에 XADD한다.
3. bulk HTTP 200만 보고 성공으로 세지 않는다: 항목별 status로 ACK 대상을 정한다.
4. relay/indexer는 서로 다른 최소 권한 DB 계정으로 동작한다(relay: outbox SELECT/UPDATE, indexer: receipt INSERT/SELECT). 코드·로그에 자격 증명이 없다.
5. unit test(fake)와 통합 시나리오가 모두 통과하고, 각 시나리오의 기대/실측 건수를 R에 기록한다.

범위 밖: producer(auth/api) Outbox INSERT 구현, Compose 서비스 정의(zero-trust-architecture 담당), edge-collector, 운영 ES 보안 설정, 부하·지연 성능 측정.

## A — 어떻게 (Action)

### 계획

1. `pipeline/relay/`: polling relay.
   - 짧은 transaction(READ COMMITTED): `status='PENDING' AND (lease_until IS NULL OR lease_until < UTC_TIMESTAMP(6))` 을 `ORDER BY id LIMIT N FOR UPDATE SKIP LOCKED` 로 잡고 `lease_owner/lease_until/attempts+1` 갱신 → commit.
   - commit 뒤 XADD(`event_id`, `payload`) → 성공한 행만 `lease_owner = 나` 조건으로 PUBLISHED 표시.
   - 시각은 DB의 `UTC_TIMESTAMP(6)` 하나를 기준으로 한다(여러 relay 간 시계 차이 제거). lease owner는 `호스트명:pid:난수` 로 프로세스마다 고유.
   - batch 크기·poll 간격·lease 초는 env. `/metrics`(published, lag, attempts histogram), `/healthz`.
2. `pipeline/indexer/`: XREADGROUP(block, count) → C-02 검증기(`contracts/tools/validate.py`) 재사용 → poison은 DLQ(`zetty:security-events:dlq`)에 event_id·규칙 ID만 XADD 후 XACK → ES `_bulk`(`index`, `_id=event_id`, index는 occurred_at UTC 날짜) → **항목별** 결과 확인 → 성공 항목만 receipt `INSERT IGNORE` → 그 stream ID만 XACK. 실패 항목은 pending으로 남겨 XAUTOCLAIM(idle > 설정 ms)으로 재시도. 기동 시 자기 PEL(`0`)부터 처리. `NOGROUP`이면 group을 `0`부터 다시 만든다(Redis 유실 복구).
3. ES index template `pipeline/es/security-events-v2-template.json`: C-02 필드와 1:1인 strict mapping(id·enum은 keyword, 시각은 date). 로그에는 ES 오류 `type`/status만 남기고 `reason`(문서 값이 포함될 수 있음)은 남기지 않는다.
4. 복구 CLI `pipeline/recovery/`: receipt 없는 PUBLISHED 행(또는 전체)을 PENDING으로 되돌리는 replay 명령. 운영 계정(outbox 상태 컬럼 UPDATE + receipt SELECT)으로 실행.
5. 설정은 env(`MYSQL_*`, `REDIS_EVENTS_*`, `ES_URL` 등), 비밀은 `*_FILE` 경로도 지원. Dockerfile(python:3.12-slim, non-root, 버전 고정 requirements).
6. 테스트: fake 기반 unit test(lease/claim, 항목별 bulk 결과, poison→DLQ, receipt 멱등) + `p02test-` 접두사 일회용 컨테이너 통합 시험(a~g). trap으로 항상 정리, ES는 한 번에 하나(heap 512m).

### 검토한 대안과 선택 이유

| 선택지 | 장점 | 단점 | 결정 |
|---|---|---|---|
| **polling relay (SKIP LOCKED + lease)** | outbox 표 하나에 대한 SELECT/UPDATE 권한만 필요. 추가 런타임 없음. 여러 relay 병렬 가능 | poll 간격만큼 지연, 상태 UPDATE 쓰기 증가 | **채택** |
| CDC(Debezium, binlog) | poll 부하 없음, commit 순서 보존, 지연 낮음 | binlog ROW·REPLICATION 권한 필요(outbox 외 전체 binlog를 읽는 넓은 권한), Kafka Connect/Debezium Server JVM·offset 저장소 운영. 단일 호스트 lab에 과함 | 기각 |
| **Redis Streams** | consumer group·PEL·XAUTOCLAIM으로 항목별 ACK·회수. COMPOSE의 `redis-events` 와 일치 | 단일 인스턴스, 유실 가능(→ Outbox 재발행으로 복구) | **채택** |
| Kafka | 복제 로그, offset replay | broker(KRaft) JVM ~1GB, 단일 호스트에서는 내구성 이점이 없음 | 기각 |
| relay가 ES에 직접 쓰기 | hop 최소 | relay가 ES 쓰기 권한·검증·재시도까지 떠안음. ES 장애가 Outbox lag로 직결, 다른 consumer(uba-worker) fan-out 불가 | 기각 |
| **at-least-once + 멱등 적재** (`_id=event_id`, receipt `INSERT IGNORE`) | 재전송·재처리를 모두 허용하면서 ES 결과 중복 0 | 같은 event_id에 다른 내용이 오면 `index` 가 덮어씀 | **채택**. "exactly-once 전달"로 표현하지 않는다 |
| exactly-once 주장 | 단순한 설명 | MySQL→Redis→ES 사이에 공통 transaction(2PC)이 없어 참이 아님 | 기각 |
| claim transaction REPEATABLE READ(기본) | 설정 불필요 | locking read가 next-key(gap) lock을 잡아 producer INSERT를 막을 수 있음 | 기각. relay 세션은 READ COMMITTED |
| poison을 pending으로 둠 | 구현 단순 | 영원히 재시도·PEL 증가 | 기각. DLQ + ACK(원본은 MySQL Outbox에 남음) |

### 시행착오

(진행 중 추가)

## R — 개선 결과 (Result)

미측정.

## 자소서 한 줄 (R 확정 후)

미작성.
