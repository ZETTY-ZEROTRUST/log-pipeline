# P-01 C-02 공통 계약 JSON Schema·golden fixture

- 상태: 진행 (Python 검증 완료, Java 동일 revision 검증 미실행)
- 연결: Jira C-02 · 명세 `docs/contracts.md`(owner 문서, main tree) · `uba-analyzer/docs/ml/IMPLEMENTATION.md` §5 · `docs/PLAN_INFRA_AUTH.md` Phase 2
- 작성/갱신: 2026-09-27 (계획), 2026-09-27 (R 기록)

## S — 문제 발생 (Situation)

- `docs/contracts.md`는 SecurityEvent v2 / AnomalyDetection v1 / ResponseCommand v1의 **의미**만 정의한다. 문서 머리말(`contracts.md:3`)이 "JSON Schema/golden fixture 파일은 아직 생성되지 않았다"고 명시한다.
- 기계 검증 파일이 없어서 producer(backend/edge, Java)와 consumer(uba-analyzer, Python)가 각자 해석한다. 다음 리스크가 있다.
  - 검증 실패 요청의 actor/token_ref가 채워져 학습 신원이 오염될 수 있다(`contracts.md:43`).
  - 원문 JWT·cookie·전체 query·UA 원문이 자유 문자열 필드로 유입될 수 있다(`contracts.md:48`).
  - 미평가(status≠EVALUATED) 탐지에 score/flag가 채워져 consumer가 이상으로 오해할 수 있다(`contracts.md:99`).
  - 만료·stale response command가 재실행될 수 있다(`contracts.md:114`).
- 기존 `filebeat-*`/`uba-*` 매핑은 구 계약이며 raw JWT를 ingest에서 분해한다(`README.md` 30초 요약). 새 계약을 여기에 섞을 수 없다(`contracts.md:12`).
- Phase 6 producer 연결은 C-02 고정 이후에만 가능하다(`docs/PLAN_INFRA_AUTH.md` Phase 2). 현재 이 작업이 후속 작업의 선행 조건이다.

## T — 왜 / 목표 (Task)

성공 기준(모두 명령 실행 결과로 확인한다).

1. `contracts/security-event/v2/`, `contracts/anomaly-detection/v1/`, `contracts/response-command/v1/`에 draft 2020-12 JSON Schema가 있다. 모든 object는 `additionalProperties: false`다.
2. 신뢰 규칙을 조건부 schema로 강제한다: 검증 실패 시 actor/token_ref=null, edge는 신원을 승격하지 않음, result↔reason 허용목록, 미평가 score/flag=null, ENFORCE는 expected_state_version 필수.
3. 원문 비밀 금지를 필드별 문자 집합 제한 + JWT 패턴 부정 검사로 강제하고, 패턴으로 막을 수 없는 한계를 문서화한다.
4. `contracts.md` §7의 모든 예제(정상 Auth/READ/WRITE, authn 실패, 미발급 digest, authz 거부, rollback, edge 완료, duplicate event, API/edge join, out-of-order/late, missing bytes, UNKNOWN network, model unavailable, response dry-run/retry/stale)가 fixture로 존재한다.
5. invalid fixture는 규칙 하나만 위반하고 파일 이름이 그 규칙을 나타낸다. 테스트가 "위반 규칙이 정확히 하나"임을 검사한다.
6. JSON Schema로 표현할 수 없는 의미(event_id 중복 충돌, request_id 중복 집계, late/out-of-order 분류, UTC index 날짜, detection_id 수렴, score↔flag 일치, command 만료 후 집행)를 Python 코드로 검사한다.
7. `contracts/MANIFEST.json`이 schema·fixture 파일의 sha256과 전체 revision hash를 고정하고, 테스트가 현재 파일과 일치함을 확인한다.
8. 모든 valid fixture 통과, 모든 invalid fixture 실패. JSON parse 성공만으로 의미 계약 검증을 주장하지 않는다(`contracts.md:133`).

범위 밖: Java 쪽 검증 실행, ES mapping/index template, producer/consumer 구현, Docker/Compose 실행.

## A — 어떻게 (Action)

### 계획

1. 공통 `$defs`(UUID, UTC 시각, 가명 key, version token, no_raw_jwt)를 schema 안에 정의한다. 파일 간 `$ref`를 쓰지 않아 각 schema가 단독으로 Java/Python validator에 로드되게 한다.
2. SecurityEvent v2: 필드 표를 그대로 required로 두고 null 허용 필드는 명시적 null을 요구한다. 신뢰·평가 순서·결과·event_type별 계약을 top-level `allOf`의 `title` 붙은 if/then 규칙으로 분리한다. 규칙 이름을 테스트가 위반 규칙 식별자로 사용한다.
3. AnomalyDetection v1: status별 score/flag/threshold 조합, window 또는 event_ref 중 하나, 공개 dataset target 격리. detection_id는 정해진 필드의 sha256으로 결정한다.
4. ResponseCommand v1 + 실행 결과 schema: action↔target 조합, ENFORCE의 state version 필수, status↔reason↔applied_at 조합.
5. fixture: `fixtures/valid`, `fixtures/invalid`, 여러 문서 사이 의미를 검사하는 `fixtures/scenarios`, 그리고 각 fixture의 schema·기대 결과·위반 규칙을 기록한 `fixtures/index.json`.
6. `contracts/tools/validate.py`(schema + 의미 검사 CLI), `contracts/tools/hash.py`(MANIFEST 재생성/검사), `contracts/tests/test_contracts.py`(stdlib unittest).
7. 의존성: 시스템 python3에 `jsonschema`가 없으면 `contracts/.venv`에만 설치한다. 전역 설치 금지. `.venv/`는 `.gitignore`.
8. `contracts/README.md`에 구조·검증 방법·버전 올리는 절차·한계를 정리한다.

### 검토한 대안과 선택 이유

| 선택지 | 장점 | 단점 | 결정 |
|---|---|---|---|
| 규칙을 코드(Python)로만 검사 | 표현력 높음 | Java가 같은 규칙을 재구현해야 하고 해석이 갈림 | 기각. 가능한 규칙은 schema에 둔다 |
| **schema if/then + 표현 불가 규칙만 코드** | 두 언어가 같은 schema 파일로 대부분 검증 | if/then 오류 메시지가 장황함 | **채택**. 규칙에 `title`을 붙여 위반 식별 |
| 공통 `$defs`를 별도 파일로 두고 `$ref` | 중복 제거 | Java/Python validator의 원격/상대 `$ref` 해석 설정이 달라짐 | 기각. schema별 자급자족 |
| `format`(uuid/date-time)만 사용 | 간결 | 2020-12에서 format은 기본 annotation이라 validator마다 검사 여부가 다름 | 기각. `pattern`으로 강제하고 format은 보조 |
| 원문 JWT를 금지하는 전용 필드 목록 | 단순 | 새 자유 문자열 필드가 생기면 누락 | 기각. 모든 문자열 `$defs`에 공통 부정 패턴 적용 |
| hash 대상에 schema만 포함 | 요청 최소 | fixture가 바뀌어도 revision이 같음(`contracts.md:133`은 schema/fixture 동일 revision 요구) | schema + fixture 모두 포함 |
| pytest 사용 | 편의 | 추가 의존성 | stdlib unittest로 작성(pytest로도 실행 가능) |

### 시행착오

- 2026-09-27: 시스템 python3(3.9.6)에 `jsonschema`가 없어 `contracts/.venv`에만 jsonschema 4.25.1 + rfc3339-validator 0.1.4를 설치했다(`contracts/requirements.txt`에 고정). date-time format 검사는 rfc3339-validator가 없으면 조용히 꺼지므로 테스트가 FormatChecker 활성 여부를 확인한다.
- 2026-09-27: nullable 필드(`anyOf: [def, null]`) 오류가 부모 위치로만 보고돼 위반 필드를 특정할 수 없었다. type 불일치로 탈락한 branch를 제외하고 하위 오류로 내려가 위치를 구하도록 `validate.py`의 `_leaf_errors`를 작성했다.
- 2026-09-27: Python 3.9의 `datetime.fromisoformat`은 `Z` 접미사와 1~6자리 소수 초를 처리하지 못한다. schema 패턴과 같은 형식만 받는 전용 파서(`parse_utc`)를 두었다.
- 2026-09-27: 규칙을 하나씩 약화하는 mutation 점검에서 `identity_requires_verified_authn`의 authn 조건만 느슨하게 하면 MANIFEST 테스트 외에는 실패하지 않았다. 기존 invalid fixture가 issuance 조건 때문에 여전히 실패했기 때문이다. authn 조건을 독립적으로 고정하는 `invalid/login_failed_but_actor_present.json`(발급 검사 NOT_APPLICABLE인 로그인 실패 + actor)을 추가해 검출되게 했다.

## R — 개선 결과 (Result)

측정 환경: macOS(Darwin 25.4.0), `contracts/.venv` Python 3.9.6, jsonschema 4.25.1, 단일 실행, 2026-09-27. 동시에 부하 시험이 돌고 있어 시간 값은 참고용이다.

| 항목 | 전 | 후 |
|---|---|---|
| 기계 검증 schema | 0 (`contracts.md:3`) | 4 (security-event/2.0, anomaly-detection/1.0, response-command/1.0, response-result/1.0) |
| fixture(index 항목) | 0 | 111 (valid 35, invalid 60, scenario 16) + index.json 3 |
| §7 예제 커버 | 0/17 | 17/17 (`test_section7_examples_are_covered`) |

fixture 세부:

| 계약 | valid | invalid(parse/schema/semantic) | scenario valid/invalid |
|---|---|---|---|
| security-event/v2 | 16 | 1 / 33 / 0 | 3 / 3 |
| anomaly-detection/v1 | 9 | 0 / 10 / 4 | 2 / 0 |
| response-command/v1 | 10 | 0 / 9 / 3 | 4 / 4 |

실행 명령과 결과:

```text
$ contracts/.venv/bin/python -m unittest discover -s contracts/tests -v
Ran 24 tests in 2.283s
OK                                   (exit 0)

$ contracts/.venv/bin/python contracts/tools/validate.py --fixtures
fixtures checked: 111, OK            (exit 0)

$ contracts/.venv/bin/python contracts/tools/hash.py --check
MANIFEST.json OK (sha256:30ced95365487033d924fdbcd956f9fea629b09b997fd747e52466fddfae8e50)   (exit 0)
```

- invalid fixture 60개 모두 index에 적힌 규칙 **하나만** 위반함을 테스트가 확인했다.
- valid fixture의 모든 문자열 위치에 가짜 JWT/`Bearer` 값을 넣는 탐침 전부가 schema에서 거부됐다(`test_no_string_field_accepts_raw_jwt_or_bearer`).
- mutation 점검(작업 사본에서 실행): top-level 규칙 7개(edge_observation_only, outcome_matches_checks, token_ref_requires_actor, unevaluated_has_no_score_or_flag, public_dataset_target_isolation, enforce_requires_state_version, applied_at_only_when_applied)를 각각 제거하면 MANIFEST 외 테스트가 2~4개씩 실패했다. version_token의 원문 비밀 부정 패턴을 제거하면 SecretGuard·invalid fixture 테스트가 실패했다.
- MANIFEST revision: `sha256:30ced95365487033d924fdbcd956f9fea629b09b997fd747e52466fddfae8e50`.

미검증(완료 조건 중 남은 것):

- **Java 쪽에서 같은 revision의 schema/fixture로 같은 결과가 나오는지 실행하지 않았다.** `contracts.md` §7의 "Java/Python이 같은 schema/fixture revision/hash로 통과"는 아직 충족되지 않았다.
- owner 문서 §2 합성 예제의 `"token_ref": "demo-jti"`는 이 schema에서 거부된다(object로 결정). owner 문서 갱신이 필요하다.
- producer/consumer 연결, ES mapping, CI 연결은 범위 밖이며 실행하지 않았다.

## 자소서 한 줄 (R 확정 후)

미작성. Java 동일 revision 검증 후 작성한다.
