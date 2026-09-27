# P-02 Outbox relay → Redis Streams → Elasticsearch indexer

- 상태: 완료 (일회용 컨테이너 시험 환경 검증. Compose 연결·producer 연결 전)
- 연결: Jira A-06 · I-02 · 공유 계약 `zetty-wt/shared/outbox-contract.md` · 명세 `docs/contracts.md` §6(owner 문서, main tree) · `zero-trust-architecture/docs/COMPOSE.md` §6 · 선행 [P-01](P-01-c02-contracts.md)
- 작성/갱신: 2026-09-27 (계획), 2026-09-27 (구현·R 기록)

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

- 2026-09-27: `pipeline/redis/`(ACL 규칙 디렉터리) 때문에 ruff isort가 `import redis`를 first-party로 분류했다. 작업 디렉터리에 따라 라이브러리 import가 가려질 위험도 있어 import할 수 없는 이름 `pipeline/redis-events/`로 바꾸고 isort 구역을 명시했다.
- 2026-09-27: Dockerfile에 `# syntax=docker/dockerfile:1`을 두면 build 때 frontend 이미지를 네트워크에서 받는다. 지시문을 빼고 Dockerfile별 `Dockerfile.dockerignore`(allowlist)만 사용했다. 이미지 안 파일 목록으로 허용 파일만 들어감을 확인했다(indexer: contracts 3개 + pipeline 코드).
- 2026-09-27: index 이름이 문서마다 다르다. 공유 계약·과제는 `security-events-v2-YYYY.MM.DD`, `docs/contracts.md` §3 초안과 `contracts/tools/validate.py:54`는 `zetty-security-events-v2-`. 공유 계약을 따르고 `ES_INDEX_PREFIX`로 바꿀 수 있게 했다(미해결, 소유자 결정 필요).
- 2026-09-27: `COMPOSE.md` §6은 poison 사유를 **내구성 저장소**에 남기라고 하지만 공유 계약은 Redis DLQ + ACK다. 공유 계약대로 구현하고, 원문은 MySQL Outbox에 남으며 `recovery status`의 `published_without_receipt`로 다시 찾을 수 있게 했다. 사유 코드의 내구 저장은 계약 변경이 필요해 남겨 둔다.
- 2026-09-27: relay에 outbox **컬럼 단위** `UPDATE`만 주면 `SELECT … FOR UPDATE SKIP LOCKED`가 허용되는지 확실하지 않았다. 통합 시험에서 `zetty_relay`(outbox SELECT + 상태 컬럼 5개 UPDATE)로 전 시나리오가 통과해 컬럼 단위 권한으로 충분함을 확인했다.
- 2026-09-27: ES `date`(`strict_date_optional_time`)가 6자리 소수 초(`…59.999999Z`)를 받는지 확실하지 않았다. 시나리오 f에서 적재·index 날짜 모두 정상.
- 2026-09-27: 통합 2회차 뒤 오래 쉰 MySQL 연결(wait_timeout)을 사용 전에 ping하도록 `LazyMySQL`을 고쳤다. 그래서 R은 최종 커밋(`9c8cbe6`)에서 다시 2회 실행한 결과만 쓴다.
- 2026-09-27: 1·2·3회차 시작 시 k6 부하 시험 컨테이너가 돌고 있어 `run.sh`가 끝날 때까지 기다린 뒤 컨테이너를 띄웠다. 시간 값은 참고용이며 R은 건수만 비교한다.

## R — 개선 결과 (Result)

측정 환경: macOS(Darwin 25.4.0), Docker 29.6.1(Docker Desktop, 12 CPU, 약 7.7 GiB), 일회용 컨테이너 `mysql:8.0` · `redis:7-alpine` · `elasticsearch:8.19.14`(single-node, security off, heap 512m) · relay/indexer 이미지(`python:3.12-slim` digest 고정), 호스트 시험 Python 3.12.13. 코드 `9c8cbe6`. 실행 2회(3·4회차), 두 회차의 모든 건수가 같았다. 결과 파일: [`results/P-02-integration-report.json`](results/P-02-integration-report.json)(4회차).

```text
$ PYTHON=<py3.12 venv>/bin/python pipeline/tests/integration/run.sh
10 passed in 53.84s   (3회차, exit 0, 종료 후 p02test 컨테이너 0)
10 passed in 51.77s   (4회차, exit 0, 종료 후 p02test 컨테이너 0)
$ python -m pytest pipeline/tests/unit        → 44 passed (exit 0)
$ python -m ruff check pipeline               → All checks passed (exit 0)
```

| # | 시나리오 | 기대 | 실측 |
|---|---|---|---|
| a | 정상 200건(relay 2 + indexer 2 동시) | 문서 200, receipt 200, PUBLISHED 200, stream 200, pending 0 | 문서 200, receipt 200, PUBLISHED 200, stream 200(중복 발행 0), pending 0, ES `_source`=원본 200/200, attempts 전부 1, index mapping `dynamic=strict` |
| b | relay가 XADD 후 표시 전 종료(60건, batch 20, lease 3s) | stream 80, 문서 60, receipt 60 | 종료 코드 86, stream 80, 문서 60(고유 60), receipt 60, attempts=2 행 20, relay-2 attempts histogram ≤1: 40 / ≤2: 60 |
| c1 | indexer가 bulk 직후 종료 → 같은 consumer 재기동(60건) | 종료 직후 문서 25·receipt 0·pending 25 → 문서 60 | 종료 직후 25/0/25 → 문서 60(고유 60), receipt 60, pending 0, `_version`=2 문서 25(덮어쓰기) |
| c2 | indexer가 receipt 직후 종료 → 다른 consumer XAUTOCLAIM | 종료 직후 25/25/25 → 문서 60, receipt 60 | 종료 직후 25/25/25 → 문서 60, receipt 60, pending 0, 회수 25, 새 receipt 35(중복 0) |
| d | ES 부분 실패(닫힌 index 20 + write block 20 + 정상 20) | 실패 중 receipt 20·pending 40, 해소 후 60 | 실패 중 receipt 20, pending 40, pending event_id = 실패 40건과 일치, `index_closed_exception` 40·`cluster_block_exception` 40(재시도 포함) → 해소 후 문서 60, receipt 60, pending 0 |
| e | Redis FLUSHALL(40 적재 + 40 Redis에만) → replay | 복구 대상 40, 문서 80; 실행 중 재유실 후 +20 → 100 | 유실 후 stream 0·문서 40, `published_without_receipt` 40, dry-run 40, reset 40 → 문서 80·receipt 80; 실행 중 FLUSHALL + 20 → 문서 100, receipt 100, pending 0 |
| f | 늦은 이벤트(어제 23:59:59.999999Z, 오늘 00:00Z, 지금) | 어제/오늘/오늘 index | `security-events-v2-2026.09.26` / `…2026.09.27` / `…2026.09.27`, receipt의 es_index 동일 |
| g | poison 2 + 정상 5 | DLQ 2(event_id·rule만), 문서 5, receipt 5 | DLQ 2, 필드 `{event_id, rule}`, rule `schema:#/classification_version`·`schema:#/occurred_at`, `eyJ` 0건, 문서 5, receipt 5, pending 0, `published_without_receipt` 2 |
| i | redis-events를 pause한 채 relay 기동(10건) | lease commit 후 XADD 대기 중 다른 세션이 NOWAIT로 잠금 가능 | lease 10행 commit, `FOR UPDATE NOWAIT` 10행 성공(=relay가 lock 미보유) → 해제 후 문서 10, receipt 10 |
| h | 최소 권한 | 모두 거부 | relay의 payload UPDATE·INSERT·receipt 조회, indexer의 outbox 조회·receipt DELETE, Redis relay의 FLUSHALL·XRANGE, indexer의 본 stream XADD·다른 키 GET 9건 모두 거부 |
| - | 컨테이너 로그의 비밀번호 | 0 | 23개 컨테이너 로그에서 이번 실행 비밀번호 0건 |

전/후(전 = v1 코드 확인 사실, 측정값 아님):

| 항목 | 전(v1 Filebeat tail) | 후(P-02) |
|---|---|---|
| 원문 Authorization 수집 | 기록함(`nginx-pep/uba.conf:34`) | 수집 경로에 없음. payload는 C-02 검증 통과본만 적재(위반은 DLQ, 값 미기록) |
| 중복 판정 ID | 없음(ES 자동 ID) | `_id = event_id`: 재발행 20건(b)·재처리 25건(c1/c2)에도 문서 수 = 고유 event_id 수 |
| 적재 확인 | 없음 | receipt 표: 모든 시나리오에서 receipt 수 = 문서 수 |
| 유실 복구 | 불가 | Outbox replay로 Redis 유실 40건 복구(e) |

한계(측정하지 않은 것): 건수는 최대 200건 lab 규모이며 처리량·지연·장시간 운전은 측정하지 않았다. Compose 서비스로 연결해 producer(auth/api)의 실제 Outbox INSERT와 함께 돌려 보지 않았다.

## 자소서 한 줄 (R 확정 후)

MySQL Outbox → Redis Streams → Elasticsearch 전달 경로를 at-least-once + 멱등 적재(`_id=event_id`, receipt)로 구현하고, relay/indexer 강제 종료·ES 부분 실패·Redis 유실 등 9개 시나리오(장애 재현 7개)를 일회용 컨테이너로 2회 반복 실행해 모두 중복·유실 0건(ES 문서 수 = 고유 event_id 수)을 확인했다.
