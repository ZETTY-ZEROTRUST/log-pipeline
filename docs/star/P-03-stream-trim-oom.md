# P-03 이벤트 스트림 무한 증가 → noeviction OOM으로 파이프라인 정체 (색인 후 트림)

- 상태: 완료
- 연결: `P-02`(relay·indexer) · infra `Z-05`(파이프라인 통합) · backend `B-08`(RESPONSE_APPLIED 집행 이벤트) · L-06(감사 이벤트 비용)
- 작성/갱신: 2026-09-27

## S — 문제 발생

I-04 집행 루프의 종단(집행→RESPONSE_APPLIED→ES)을 확인하려다 발견했다. 부하 시험으로 22만 건을 흘린 뒤 파이프라인이 **완전히 멈춰 있었다.**

- redis-events: `--maxmemory 256mb --maxmemory-policy noeviction`(조용한 유실을 막으려 noeviction 선택).
- 스트림 `zetty:security-events` **XLEN=222,213**, `used_memory=256.06M = maxmemory`.
- relay 로그: `xadd failed rows=100; will retry after lease expiry` 반복. XADD가 `OOM command not allowed when used memory > 'maxmemory'`로 거부됨.
- 결과: Outbox PENDING **205,075건이 배출되지 않음**. 새 이벤트(RESPONSE_APPLIED 포함)가 전송 버퍼에 못 들어가 ES까지 못 감.
- indexer는 소비 그룹 기준 `pending=0, lag=0` — **22만 건 전부 이미 ES에 색인·ACK 완료**인데도 스트림에 그대로 남아 메모리를 다 쓰고 있었다.
- indexer를 재기동해도 **stage=startup error=OutOfMemoryError**로 복구 못 함: `XGROUP CREATE`·`XREADGROUP`은 `denyoom` 쓰기라 메모리가 꽉 차면 거부되어, 읽기 루프 진입 자체가 불가.

## T — 목표

- 색인이 끝난(ES 저장·ACK된) 항목이 전송 버퍼에 무한 적체되지 않게 **버퍼를 색인 진도에 묶는다.**
- 단, noeviction의 취지(미색인 이벤트의 조용한 유실 방지)를 지킨다 — **미ACK·미읽음 항목은 절대 제거하지 않는다.**
- 메모리가 이미 꽉 찬 인스턴스도 **재기동만으로 스스로 회복**한다.

## A — 어떻게

### 검토한 대안과 선택 이유

| 방식 | 유실 위험 | 판단 |
|---|---|---|
| relay `XADD ... MAXLEN ~ N`(생산 측 상한) | indexer가 밀리면 **미색인 항목이 잘림** → 조용한 유실 | 제외(noeviction 취지 위반) |
| maxmemory 상향 | 시점만 미룸, 근본 해결 아님 | 제외 |
| `allkeys-lru` 등 eviction 정책 | 미색인 이벤트도 evict → 유실 | 제외 |
| **indexer가 ACK 후 `XTRIM MINID`로 트림** | 하한을 미ACK보다 아래로만 두면 유실 없음 | **채택** |

### 구현

- `RedisStreamConsumer`에 `oldest_pending_id()`(XPENDING, 읽기), `last_delivered_id()`(XINFO GROUPS, 읽기), `trim_min_id()`(XTRIM MINID, 메모리 회수) 추가.
- 트림 하한 = `가장 오래된 pending id`, 없으면(`pending=0`) `그룹 last-delivered-id`. **미ACK·미읽음은 항상 그 하한보다 크므로 보존**된다(유실 없음).
- 실행 시점: (1) **startup에서 가장 먼저** — `ensure_group`/`read`는 denyoom이라 OOM 상태에서 막히지만, 트림은 읽기+회수 연산이라 동작한다. 그룹이 없으면(신규 스트림·메모리 여유) NOGROUP을 무시하고 정상 경로로. (2) claim 주기(기본 15s)마다 정기 트림.
- ACL: `zetty-indexer`에 `+xtrim +xinfo` 부여(스트림 keyspace 내). relay·default 권한은 그대로.

## R — 개선 결과 (실측)

indexer 재빌드·재기동(코드 + ACL LOAD) 직후:

| 시점 | used_memory | 스트림 XLEN | Outbox PENDING |
|---|---|---|---|
| 재기동 전 | 256.06M(=cap) | 222,213 | 205,075 |
| startup 트림 직후 | **11.6M** | 8,901 | 196,075 |
| +14s | 33.6M | 28,301 | 176,475 |
| +28s | 102.2M | 88,801 | 108,175 |
| +42s | 219.7M | 190,476 | **0** |
| 안정 | ~200M(정기 트림) | 감소 유지 | 0 |

- startup 트림 로그: `trimmed indexed stream entries=222212 min_id=1790493841034-12`(= 그룹 last-delivered-id). 이후 15s마다 `trimmed ... entries=7300` 정기 트림.
- **메모리 256M→11.6M 회수 → relay XADD 재개 → Outbox 205,075건이 약 42초에 전량 배출(PENDING=0)**. 이후 메모리는 ~200M에서 안정(정기 트림), XADD 정상.
- RESPONSE_APPLIED: Outbox `PUBLISHED=4` → 재적재 백로그(receipt로 dedup)를 오래된 것부터 배출한 뒤 **ES 색인 도달 확인(ES count=4, 스트림 XLEN≈0)**. DLQ=0(거부·유실 아님). I-04 종단(집행→감사→ES) 성립.
- 유실 없음: 트림 하한이 항상 미ACK·미읽음보다 아래이며 DLQ=0. 단위 테스트 3건(가장 오래된 pending 하한 사용·pending 0이면 last-delivered 사용·둘 다 없으면 no-op) + 기존 44건 통과, ruff clean.

## 자소서 한 줄
색인이 끝난 이벤트가 전송 버퍼(Redis Stream)에 무한 적체돼 noeviction 정책상 XADD가 OOM으로 막히고 파이프라인이 20만 건 적체로 정지한 장애를, "색인·ACK 지점까지만 MINID 트림"으로 해결해 유실 없이 메모리를 256M→11.6M 회수하고 적체를 42초 만에 0으로 배출했습니다.
