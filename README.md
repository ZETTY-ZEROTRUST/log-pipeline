# 📡 ZETTY log-pipeline — 이벤트 전달 파이프라인 + v2 탐지 본체

> **ZETTY-ZEROTRUST — 아주대 캡스톤 / Google × Ajou AI Capstone Design**
> SecurityEvent v2 이벤트를 **업무 DB → ES 까지 안전하게 운반**하고, 운반된 이벤트로 **이상 탐지 → 사건 보고**까지 수행하는 레포.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg)](#)
[![Redis Streams](https://img.shields.io/badge/Redis-Streams-DC382D.svg)](#)
[![ES](https://img.shields.io/badge/Elasticsearch-8.x-005571.svg)](#)
[![IsolationForest](https://img.shields.io/badge/Detection-IsolationForest-brightgreen.svg)](#)
[![ZT](https://img.shields.io/badge/Zero%20Trust-Detect%20%2B%20Report-blueviolet.svg)](#)

---

## ⚡ 30초 요약

**log-pipeline 은 ZETTY 의 _탐지 본체_** 다. 두 가지를 소유한다.

1. **v2 이벤트 전달 파이프라인** — 업무 DB의 Transactional Outbox → Redis Streams → Elasticsearch. `at-least-once + 멱등 적재`. C-02 계약 검증과 DLQ(poison) 격리, MINID 트림 포함. → [`pipeline/`](pipeline/README.md)
2. **v2 공통 계약(C-02)** — `security-event/2.0`, `anomaly-detection/1.0`, `response-command/1.0`(+`response-result/1.0`)의 JSON Schema · golden fixture · 결정적 검증기. producer(backend/edge, Java)와 consumer(이 저장소 indexer·detector, Python)가 **같은 revision**으로 검증. → [`contracts/`](contracts/README.md)
3. **v2 탐지 + 보고** — 운반된 `security-event/2.0`으로 이상 탐지하고 사건을 묶어 보고한다.
   - [`pipeline/detector/detect.py`](pipeline/detector/detect.py) — HTTP 행위 이상 탐지(비지도 IsolationForest) → `anomaly-detection/1.0` + 고신뢰 이상의 `response-command/1.0`(DRY_RUN).
   - [`pipeline/detector/anomaly_incident.py`](pipeline/detector/anomaly_incident.py) — `anomaly-detection/1.0` → subject별 incident 묶기 → 결정론 보고 필드 조립 → LLM 요약(폴백 있음) → Slack 리포트.
   - [`notebooks/rba_selfcontained_train.ipynb`](notebooks/rba_selfcontained_train.ipynb) — self-contained 학습 노트북(전처리·학습이 셀에 드러남, CPU).

> 🧭 **탐지 규율**: unlabeled IsolationForest + 시간순 분할, **라벨은 평가(ground truth) 전용**, 임계는 정상 held-out의 목표 FPR 분위수로 재보정. `anomaly_score`는 공격 확률이 아니고 evidence는 관측 대 기준 비교일 뿐이다. **자동 대응 판단은 backend I-04(response-command 집행)의 몫**이고, 이 레포는 탐지 결과와 그 대응 '결과'만 옮긴다.

> ⚠️ **v1 UBA는 폐기됨.** 별도 `uba-analyzer` 레포의 7-factor · ES 런타임 · LLM ReAct 는 더 이상 쓰지 않는다. 그 keeper 들은 위 v2 탐지 코드로 이관됐다. 아래 [§6 레거시 v1 수집 파이프라인](#6-레거시-v1-수집-파이프라인-여전히-ci-배포-대상)의 Nginx PEP / Filebeat / ES ingest 자산은 파일이 남아 있고 CI가 아직 배포하지만, 그 소비자였던 v1 UBA 런타임은 없다.

---

## 🔀 1. v2 이벤트 전달 파이프라인 (`pipeline/`)

`security-event/2.0`(C-02) 이벤트를 **업무 transaction과 함께 기록된 Outbox에서 ES까지** 옮긴다. 보장은 **at-least-once 전달 + 멱등 적재**이며 exactly-once를 주장하지 않는다.

```text
producer(auth/api) ──(업무와 같은 DB transaction)──▶ MySQL security_event_outbox (PENDING)
                                                       │
outbox-relay   ① SELECT … FOR UPDATE SKIP LOCKED + lease → COMMIT (짧은 transaction)
               ② XADD zetty:security-events {event_id, payload}
               ③ UPDATE … PUBLISHED WHERE lease_owner = 나
                                                       │
redis-events   stream zetty:security-events, consumer group indexer
                                                       │
event-indexer  ① XREADGROUP(자기 PEL 우선, 주기적 XAUTOCLAIM)
               ② C-02 검증 ── 위반 ──▶ DLQ(event_id·규칙만) → XACK
               ③ ES _bulk index, _id=event_id, index=zetty-security-events-v2-<UTC occurred_at 날짜>
               ④ 성공 항목만 receipt INSERT IGNORE → 그 항목만 XACK (실패는 재시도)
```

| 경로 | 책임 |
|---|---|
| `pipeline/relay/` | lease 획득·XADD·루프·지표 |
| `pipeline/indexer/` | C-02 분류, ES bulk 항목 해석, receipt, stream 조작, 루프 |
| `pipeline/recovery/` | Outbox replay 운영 명령 |
| `pipeline/es/security-events-v2-template.json` | C-02 필드와 1:1인 strict index template |
| `pipeline/sql/least-privilege-grants.sql` | DB 계정 최소 권한(relay/indexer/ops) |
| `pipeline/redis-events/acl-rules.txt` | redis-events ACL 규칙 |
| `pipeline/tests/{unit,integration}` | fake 기반 unit test, 일회용 컨테이너 복구 시나리오 |

> 상세(환경변수·지표·복구 runbook·알려진 한계)는 [`pipeline/README.md`](pipeline/README.md), 설계·결과는 [`docs/star/P-02`](docs/star/P-02-outbox-relay-indexer.md)·[`docs/star/P-03`](docs/star/P-03-stream-trim-oom.md).

---

## 🧩 2. v2 공통 계약 (`contracts/`)

C-02 owner 문서(`docs/contracts.md`, main tree)의 의미를 **기계 검증 파일**로 고정한다. indexer가 검증기(`contracts/tools/validate.py`)를 그대로 불러 쓴다.

| 계약 | schema_version | 소유 |
|---|---|---|
| security-event | `security-event/2.0` | 이벤트(producer=backend/edge) |
| anomaly-detection | `anomaly-detection/1.0` | 탐지 산출(detector) |
| response-command / response-result | `response-command/1.0` · `response-result/1.0` | 대응 명령·결과 |

- 검사는 **parse → schema → semantic** 세 층. JSON parse/schema 통과만으로 의미 계약이 검증됐다고 하지 않는다.
- `MANIFEST.json`의 `revision`(schema·fixture sha256 연결의 sha256)으로 producer/consumer가 같은 버전을 pin 한다.
- `detection_id`는 결정적(입력 정렬 연결의 sha256)이라 재시도는 같은 ID로 수렴하고, late 자료 재평가(새 input_hash)는 새 ID를 받는다.

> 규칙 표·형식 규약·버전 올리는 절차는 [`contracts/README.md`](contracts/README.md), 설계·결과는 [`docs/star/P-01`](docs/star/P-01-c02-contracts.md).

---

## 🔎 3. v2 탐지 (`pipeline/detector/`)

### 3-1. `detect.py` — HTTP 행위 이상 탐지 (`detector_id=http-behavior`)

- **입력**: `security-event/2.0`의 `ACCESS_DECISION`을 `subject_key`별로 집계한 피처(ES agg 덤프). 가명 key만 다루며 원본 신원·secret은 읽지 않는다.
- **모델**: 비지도 `IsolationForest`(scikit-learn) + `StandardScaler`. 피처 = 요청 수 / 초당 요청률 / distinct route·session / 최대 burst.
- **출력**: 계약을 준수하는 `anomaly-detection/1.0` 레코드(결정적 `detection_id`/`input_hash`) + 평가 요약 + 고신뢰 이상에 대한 `response-command/1.0`(DRY_RUN, 정책 산출물).
- **규율**: 라벨은 학습에 쓰지 않고 평가에만 쓴다("운영에서 라벨은 없다"는 UBA 전제).

### 3-2. `anomaly_incident.py` — 사건 묶기 → 보고

- LLM은 '공격 판정자'가 아니라 '탐지 결과·근거를 정리하는 분석 보조'다.
- 같은 subject의 관련 anomaly를 incident로 묶고(경보 남발 방지), 보고 필수 필드(대상·시간·탐지결과·현재행동/비교기준/추가근거·대응결과·확인경로)를 **결정론적으로** 조립한다.
- 확정 표현을 제한하고 입력을 새니타이즈(JWT/Bearer 원문 유입 이중 차단)한 뒤, 요약을 LLM(주입)으로 쓰되 **LLM이 없거나 실패해도 결정론 폴백으로 보고가 나간다**.
- 자동 대응은 정책 코드(`response-command` / backend I-04)가 결정하고, 이 모듈은 그 '결과'만 옮긴다.

### 3-3. `notebooks/rba_selfcontained_train.ipynb` — self-contained 학습

- github clone 없이 각 셀에서 전처리·학습을 직접 보여준다. CPU IsolationForest.
- 누수 차단 규율: unlabeled fit + 시간순 분할(embargo), 라벨은 평가에만, 임계는 정상 held-out의 목표 FPR(`TARGET_FPR`) 분위수.
- 데이터: Wiefling RBA 로그인 데이터셋(Zenodo 6782156). `ROW_CAP`으로 스트리밍(메모리 안전).
- ZETTY 적용 시 임계는 ZETTY 정상 트래픽으로 **재보정**한다.

---

## 🗂️ 4. 디렉토리 구조

```text
log-pipeline/
├── pipeline/                # ▶ v2 이벤트 전달 파이프라인 + 탐지
│   ├── relay/               #   Outbox → Redis Streams 전달
│   ├── indexer/             #   Redis Streams → ES 색인(C-02 검증·MINID 트림)
│   ├── recovery/            #   Outbox replay 운영 명령
│   ├── detector/            #   detect.py(IsolationForest) · anomaly_incident.py(사건 보고)
│   ├── es/                  #   security-events-v2 strict index template
│   ├── sql/, redis-events/  #   최소 권한 grant · ACL
│   ├── common/, tests/      #   설정·연결·런타임 · unit/integration
│   └── README.md
├── contracts/               # ▶ C-02 공통 계약(security-event/2.0, anomaly-detection/1.0, response-command/1.0)
│   ├── security-event/v2/, anomaly-detection/v1/, response-command/v1/
│   ├── tools/{validate.py,hash.py}, tests/, MANIFEST.json
│   └── README.md
├── notebooks/               # ▶ rba_selfcontained_train.ipynb (self-contained 학습)
├── docs/star/               # ▶ STAR 작업 기록(P-01 계약 · P-02 relay/indexer · P-03 stream 트림)
│
├── nginx-pep/               # ▽ 레거시 v1 수집: Nginx PEP conf (uba.conf)
├── filebeat/                # ▽ 레거시 v1 수집: Filebeat 사이드카 conf
├── es-pipelines/            # ▽ 레거시 v1 수집: ES ingest(jwt-decode chain + asn-classify)
├── es-mappings/             # ▽ 레거시 v1: filebeat-jwt-template(입력) + uba-*(폐기된 v1 UBA 산출)
├── ip-classification/       # ▽ 레거시 v1: asn-map.yaml (cgnat_kr WHITELIST 등)
├── scripts/                 # ▽ 레거시 v1 운영·검증 스크립트
├── archive/                 # ▽ v10 이전 폐기본(.bak)
└── .github/workflows/       #   deploy.yml — main push 시 ELK EC2로 레거시 v1 자산 배포(SSM git pull)
```

`▶` = 현재 v2 본체, `▽` = 레거시 v1(파일 존속, 아래 §6 참고).

---

## 🛠️ 5. Tech Stack (v2)

| Category | Stack | 비고 |
|----------|-------|------|
| **언어/런타임** | Python 3.12 | `pipeline/pyproject.toml`, 이미지 build context = 저장소 루트 |
| **Outbox 원본** | MySQL | `security_event_outbox` + `security_event_receipt` |
| **전달 버스** | Redis Streams (`redis-events`) | 세션 Redis와 분리, consumer group `indexer` |
| **색인** | Elasticsearch 8.x | `zetty-security-events-v2-YYYY.MM.DD`, strict template |
| **탐지** | scikit-learn IsolationForest | 비지도 + 시간분할, 라벨 평가 전용 |
| **계약 검증** | jsonschema (draft 2020-12) | `additionalProperties:false`, semantic 층, MANIFEST revision |
| **지표** | Prometheus text(`/metrics`) | relay `:9101`, indexer `:9102`(인증 없음 — 분석 네트워크 내부만) |

---

## 6. 레거시 v1 수집 파이프라인 (여전히 CI 배포 대상)

> 아래는 **v1 시절 로그 수집 경로**다. 소비자였던 v1 UBA(`uba-analyzer`)는 폐기됐지만, 이 수집 자산 파일들은 저장소에 남아 있고 `.github/workflows/deploy.yml`이 `main` push 시 ELK EC2 박스로 배포한다(`es-mappings`·`es-pipelines`·`ip-classification`·`scripts`). v2 `pipeline/`·`contracts/`·`detector/`는 이 workflow의 배포 대상이 아니다. 정리 여부는 별도 판단이 필요하므로 파일과 이 문서를 보존한다.

**경로**: Nginx PEP(`nginx-pep/uba.conf`) → Filebeat sidecar(`filebeat/`) → ES ingest pipeline(`es-pipelines/`) → `filebeat-*` 색인(`es-mappings/filebeat-jwt-template.json`).

### 6-1. ES ingest pipeline — 2 단 chain

- **`jwt-decode`**(Painless): `$http_authorization` 헤더를 `Bearer` 분리 → Base64URL 분해 → **11 클레임을 `jwt.{sub,jti,iat,exp,...,ext.LSID}`로 평탄화**. 일부는 Filebeat script processor(`filebeat-processors-jwt.yml`)로 이관, ES `filebeat-uba-final` chain이 fallback.
- **`asn-classify`**(geoip + Painless): `x_forwarded_for` 첫 IP = client_ip → GeoLite2-ASN → `ip_asn`/`ip_org`, GeoLite2-City → `ip_country`, `asn-map.yaml` 매칭 → `ip_class`(`cgnat_kr`/`cloud`/`unknown`).

### 6-2. 레거시 설계 결정 (보존)

- Painless는 yaml 동적 로드 불가 → `asn-map.yaml`의 ASN 리스트를 **Painless 코드에 정적 박음**(yaml 갱신 시 pipeline 재배포).
- GeoLite2-ASN.mmdb는 ES 노드에 수동 배치(git 커밋 금지, MaxMind 라이선스).
- `ip` 필드(=ALB IP)는 ASN 분류에 사용 금지 — `x_forwarded_for` 첫 IP만이 실 클라이언트.
- `ip_class` soft cap 30(vs hard whitelist): 정상 NAT 알람 차단 + 공격 시 결합 신호로 잡힘.

### 6-3. ES 매핑 (Option B — strict, `dynamic: false`)

| 매핑 파일 | 색인 | 소유 |
|-----------|------|------|
| `filebeat-jwt-template.json` | `filebeat-*` (composable, priority 500) | 입력 — Nginx access log |
| `uba-events.json` / `uba-risk-scores.json` / `uba-user-profiles.json` / `uba-alerts.json` / `uba-intelligence.json` | `uba-*` | **폐기된 v1 UBA 산출** (`uba-analyzer` 런타임 없음) |

> `uba-*` 매핑은 폐기된 v1 UBA(7-factor·LLM ReAct)의 산출 색인이라 현재 소비자가 없다. 파일은 CI 배포 대상이라 남겨 두었을 뿐, v2에서는 사용하지 않는다.

---

## 🔗 7. 관련 레포 (ZETTY-ZEROTRUST Org)

| 레포 | 본 log-pipeline 과의 관계 |
|------|-------------------------|
| [`backend`](https://github.com/ZETTY-ZEROTRUST/backend) | C-02 이벤트 **producer**(Java, auth/api) + JWT 발급자. 자동 대응 집행(I-04, response-command 소비) |
| [`attack-simulation`](https://github.com/ZETTY-ZEROTRUST/attack-simulation) | 승인된 로컬 대상·모의 계정에 공격 트래픽 발사 → 탐지 공격→개선 사이클의 입력 |
| [`zero-trust-architecture`](https://github.com/ZETTY-ZEROTRUST/zero-trust-architecture) | AWS Terraform IaC + Compose(`COMPOSE.md`) + 공유 계약 문서 |
| ~~`uba-analyzer`~~ | **폐기.** v1 UBA(7-factor·ES 런타임·LLM ReAct). keeper 는 본 레포 `pipeline/detector/`로 이관 |

---

## 🤝 8. 기여 가이드

### 절대 규칙 (DO NOT)

- ❌ 탐지 학습에 **라벨 사용 금지** — unlabeled fit + 시간분할, 라벨은 평가 전용.
- ❌ `anomaly_score`를 공격 확률로, evidence를 인과로 서술 금지 — 관측 대 기준 비교일 뿐.
- ❌ 탐지/보고 코드에서 **자동 대응 집행 금지** — 대응 판단은 backend I-04(response-command).
- ❌ 계약(`contracts/`) schema·fixture 변경 시 `tools/hash.py --write` 없이 커밋 금지 — MANIFEST revision 불일치.
- ❌ 토큰·개인정보 원문을 로그·보고·detector 입력에 유입 금지 — 가명(key_version)만.
- ❌ (레거시) ingest pipeline Painless에서 yaml 동적 로드 / `ip`(ALB IP)로 ASN 분류 / ES 매핑 `dynamic: true` 되돌리기 금지.

### 커밋 컨벤션

- 포맷: `<type>(<scope>): <한글 제목>` — scope: `pipeline` / `detector` / `contracts` / (레거시) `nginx`·`es`.
- 브랜치: 기능은 `develop`에서 분기, PR로 `develop` 통합. infra 작업은 [`zero-trust-architecture`](https://github.com/ZETTY-ZEROTRUST/zero-trust-architecture).

---

> **본 log-pipeline 은 ZETTY 의 _탐지 본체_ 다.**
> 업무 DB의 Outbox에서 이벤트를 안전하게 운반하고(멱등), C-02 계약으로 검증하고, 운반된 이벤트로 이상을 탐지해 사건으로 묶어 보고한다.
> "결정적 전달 + 계약 검증 + 라벨 없는 탐지 규율" — 그게 본 레포의 책임이다.
