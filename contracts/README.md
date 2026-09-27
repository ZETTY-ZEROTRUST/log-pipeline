# C-02 공통 계약 — JSON Schema·golden fixture

`docs/contracts.md`(C-02 owner 문서)의 의미를 기계 검증 파일로 고정한다. producer(backend/edge, Java)와 consumer(이 저장소 `pipeline/indexer`·`pipeline/detector`, Python)는 **같은 revision**(`MANIFEST.json`의 `revision`)의 schema·fixture로 검증한다.

> `docs/contracts.md`는 2026-09-27 기준 아직 이 저장소에 커밋되지 않았다(사용자 main tree의 미추적 파일). 의미가 충돌하면 owner 문서가 우선이며, 이 디렉터리의 결정 사항은 아래 [명세에서 새로 정한 것](#명세에서-새로-정한-것)에 모았다.

## 구조

```text
contracts/
  MANIFEST.json                     schema·fixture sha256 + 전체 revision (tools/hash.py 생성)
  requirements.txt                  검증 도구 의존성(contracts/.venv 전용)
  security-event/v2/
    schema.json                     security-event/2.0
    fixtures/index.json             fixture별 schema·기대 결과·위반 규칙·§7 예제 매핑
    fixtures/valid/*.json           통과해야 하는 문서
    fixtures/invalid/*.json         규칙 하나만 위반하는 문서(파일 이름 = 규칙)
    fixtures/scenarios/*.json       여러 문서 사이의 의미 검사(중복·join·late)
  anomaly-detection/v1/             anomaly-detection/1.0 (같은 구조)
  response-command/v1/
    schema.json                     response-command/1.0 (명령)
    result.schema.json              response-result/1.0 (실행 결과)
    fixtures/...
  tools/validate.py                 parse → schema → 의미 검사 CLI
  tools/hash.py                     MANIFEST.json 생성·검사
  tests/test_contracts.py           stdlib unittest
```

## 검증 방법

시스템 python3에 `jsonschema`가 없으면 이 디렉터리의 venv에만 설치한다(전역 설치 금지, `.venv/`는 Git 제외).

```bash
python3 -m venv contracts/.venv
contracts/.venv/bin/pip install -r contracts/requirements.txt

# 전체 테스트
contracts/.venv/bin/python -m unittest discover -s contracts/tests -v

# fixture index 기대 결과 대조
contracts/.venv/bin/python contracts/tools/validate.py --fixtures

# 임의 문서 검사(schema_version으로 계약 자동 판별, scenario 파일도 가능)
contracts/.venv/bin/python contracts/tools/validate.py path/to/event.json

# MANIFEST 일치 확인 / 갱신
contracts/.venv/bin/python contracts/tools/hash.py --check
contracts/.venv/bin/python contracts/tools/hash.py --write
```

검사는 세 층이다. **JSON parse 또는 schema 통과만으로 의미 계약이 검증됐다고 하지 않는다.**

| 층 | 내용 |
|---|---|
| parse | 중복 key·NaN/Infinity 거부(Java/Python 파서의 해석 차이 차단) |
| schema | draft 2020-12, `additionalProperties: false`, enum·null 조합, if/then 규칙, 문자 집합·원문 비밀 부정 패턴, `format`(uuid/date-time) |
| semantic | schema로 표현할 수 없는 규칙(아래 표) |

CLI 출력은 규칙 ID·위치(JSON pointer)·keyword만 보여 준다. jsonschema 메시지는 입력 값을 포함하므로 출력하지 않는다.

## 계약 요약

### security-event/2.0

top-level `allOf`의 각 규칙은 `title`을 가지며, 테스트는 이 title을 위반 규칙 ID로 사용한다.

| 규칙(title) | 내용 |
|---|---|
| `identity_requires_verified_authn` | actor≠null ⇒ authn=SUCCESS 그리고 issuance_check∈{PASSED, NOT_APPLICABLE}. 검증 실패·발급대장 불일치·상태 미확인이면 actor=null |
| `token_ref_requires_actor` | token_ref≠null ⇒ actor≠null |
| `edge_observation_only` | producer=edge ⇒ actor/token_ref/operation=null, 세 검사 NOT_EVALUATED, outcome=UNKNOWN |
| `event_type_producer` | AUTHENTICATION→auth, TOKEN_*·RESPONSE_APPLIED→auth/bff, ACCESS_DECISION→api/bff, BUSINESS_RESULT→api, EDGE_COMPLETED⇔edge |
| `check_evaluation_order` | authn → issuance_check → authz. 앞 단계가 성공하지 않으면 뒤 단계는 평가하지 않음 |
| `outcome_matches_checks` | 검사 실패 ⇔ outcome=DENIED. STATE_UNAVAILABLE(fail-closed)은 FAILED |
| `http_observation_owner` | edge ⇔ `EDGE_FINAL`, 그 밖은 `BACKEND_RESULT` |
| `request_id_presence` | AUTHENTICATION/ACCESS_DECISION/BUSINESS_RESULT/EDGE_COMPLETED는 request_id 필수, RESPONSE_APPLIED는 null |
| `access_requires_operation` | ACCESS_DECISION/BUSINESS_RESULT는 operation 필수 |
| `authentication_event_contract` | 로그인: token_ref=null, issuance NOT_APPLICABLE, authz NOT_EVALUATED |
| `token_lifecycle_contract` | TOKEN_ISSUED/REFRESHED: actor·token_ref 필수, TOKEN_REUSE ⇔ FAILED/REUSE_DETECTED |
| `business_result_contract` | BUSINESS_RESULT: actor 필수, authz=ALLOW, outcome∈{SUCCEEDED, FAILED(rollback)} |
| `response_applied_contract` | 비HTTP 집행 기록: http/operation/token_ref=null, 검사 NOT_EVALUATED |

reason 허용목록(검사별로 분할, result와의 조합은 schema가 강제):

| 검사 | 실패 result | 허용 reason | 그 밖의 result |
|---|---|---|---|
| authn | FAILURE | TOKEN_MISSING, MALFORMED, INVALID_SIGNATURE, EXPIRED, CLAIM_INVALID, INVALID_CREDENTIALS | NONE |
| issuance_check | FAILED | NOT_ISSUED, REVOKED, VERSION_MISMATCH, REUSE_DETECTED | NOT_EVALUATED: NONE 또는 STATE_UNAVAILABLE, 나머지 NONE |
| authz | DENY | SCOPE_MISSING, ROLE_MISSING, OBJECT_NOT_FOUND_OR_NOT_OWNED | NONE |

형식: UUID는 소문자 canonical, 시각은 `Z`로 끝나는 UTC(소수 초 최대 6자리), `duration_ms`·`response_body_bytes`는 정수이며 관측하지 못하면 0이 아니라 null이다.

### anomaly-detection/1.0

| 규칙(title) | 내용 |
|---|---|
| `unevaluated_has_no_score_or_flag` | status≠EVALUATED ⇒ anomaly_score=null, is_anomaly=null, evidence=[] |
| `evaluated_requires_score_threshold_flag` | EVALUATED ⇒ score·threshold·threshold_operator·is_anomaly 필수 |
| `status_reason_matches_status` | status별 status_reason 허용목록(저활동 INSUFFICIENT_DATA와 누락 INCOMPLETE_WINDOW 구분) |
| `window_xor_event_ref` | window 기반 또는 이벤트 기반 중 하나 |
| `public_dataset_target_isolation` | PUBLIC_DATASET ⇔ target_type=DATASET_ENTITY, dataset_id 필수 |

`detection_id` = sha256 hex of UTF-8 `"\n".join([...])`, 순서:

```text
zetty:anomaly-detection:id:v1, environment, data_origin.kind, data_origin.dataset_id,
detector_id, target_type, target_key.key_version, target_key.key,
window.start, window.end, event_ref, feature_version, model_version, threshold_version, input_hash
```

null은 빈 문자열이다(모든 필드의 패턴이 빈 문자열과 개행을 금지하므로 연결이 모호하지 않다). status·detected_at은 포함하지 않는다. 그래서 같은 입력의 재시도는 같은 ID로 수렴하고, late 자료를 포함한 재평가(새 input_hash)는 새 ID를 받는다. window 경계는 소수 초 없는 UTC만 허용한다(같은 순간의 표기를 하나로 고정).

### response-command/1.0 · response-result/1.0

| 규칙(title) | 내용 |
|---|---|
| `command_has_basis` | detection_id 또는 incident_id 중 하나 이상 |
| `action_target_compatibility` | LOCK_ACCOUNT→SUBJECT, REVOKE_SESSION/REQUIRE_REAUTH→SUBJECT·SESSION, RATE_LIMIT→SUBJECT·SESSION·IP |
| `enforce_requires_state_version` | ENFORCE ⇒ expected_state_version 필수(stale 판별) |
| `synthetic_is_dry_run_only` | environment=synthetic ⇒ DRY_RUN |
| `status_reason_combination` | APPLIED/ALREADY_APPLIED/DRY_RUN→NONE, REJECTED→검증 거부 reason, FAILED→ENFORCEMENT_ERROR/STATE_UNAVAILABLE |
| `applied_at_only_when_applied` | APPLIED/ALREADY_APPLIED만 applied_at |
| `mode_status_combination` | DRY_RUN 명령→DRY_RUN 또는 REJECTED, ENFORCE 명령→DRY_RUN 불가 |
| `stale_reports_observed_version` | STALE_STATE_VERSION ⇒ observed_state_version 필수 |

### 의미 검사(validate.py)

| 규칙 ID | 대상 | 내용 |
|---|---|---|
| `SE_EVENT_ID_CONFLICT` | 이벤트 batch | 같은 event_id에 다른 내용 → 서로 다른 관측을 덮어쓰기 |
| `SE_REQUEST_DOUBLE_COUNT` | 이벤트 batch | (request_id, producer, event_type)당 관측 둘 이상 |
| `SE_FUTURE_OCCURRED_AT` | 이벤트 batch | occurred_at > first_received_at + 허용 skew |
| `AD_DETECTION_ID_MISMATCH` | 탐지 | detection_id가 결정 규칙과 다름 |
| `AD_WINDOW_ORDER` | 탐지 | start < end ≤ cutoff 위반 |
| `AD_DETECTED_BEFORE_CUTOFF` | 탐지 | detected_at < cutoff |
| `AD_FLAG_INCONSISTENT` | 탐지 | is_anomaly ≠ (score GT/GTE threshold) |
| `RC_EXPIRES_NOT_AFTER_REQUESTED` | 명령 | expires_at ≤ requested_at |
| `RC_EVIDENCE_REF_MISMATCH` | 명령 | evidence_ref가 detection_id/incident_id와 다름 |
| `RR_APPLIED_AFTER_RECORDED` | 결과 | applied_at > recorded_at |
| `RR_EXECUTED_AFTER_EXPIRY` | 명령+결과 | 만료 후 APPLIED/DRY_RUN |
| `RR_DUPLICATE_APPLY` | 명령+결과 | 같은 command_id를 두 번 APPLIED |
| `RR_STALE_APPLIED` | 명령+결과 | observed ≠ expected state version인데 APPLIED |
| `RR_ALREADY_APPLIED_WITHOUT_PRIOR` | 명령+결과 | 앞선 APPLIED 없는 ALREADY_APPLIED |
| `RR_COMMAND_ID_MISMATCH`, `RR_MODE_MISMATCH` | 명령+결과 | 다른 명령의 결과, mode 불일치 |

이벤트 batch는 dedupe 후 요약도 계산한다: 최초 수신 시각 유지, 행동 수(API ACCESS_DECISION의 서로 다른 request_id), API/edge join, 사건 시각 순서, late 분류(window `[start,end)` + allowed lateness 이후 수신), UTC `occurred_at` 날짜 기반 index 이름(`zetty-security-events-v2-YYYY.MM.DD`). window 길이·lateness·skew는 계약 값이 아니라 scenario의 `params`로 주는 실험 기본값(300초/60초/30초)이다.

## fixture 형식

`fixtures/index.json`:

```json
{"path": "invalid/authn_failed_but_actor_present.json", "schema": "schema.json",
 "expect": "invalid", "layer": "schema", "rule": "identity_requires_verified_authn"}
```

- `expect`: `valid` | `invalid`. `layer`: `parse` | `schema` | `semantic`.
- `rule`: schema 규칙은 top-level `allOf`의 `title`, 필드 규칙은 위반 위치 JSON pointer(`#/classification_version`, root는 `#`), 의미 규칙은 위 규칙 ID.
- invalid fixture는 **정확히 하나의 규칙**만 위반한다. 테스트가 위반 규칙 집합이 `[rule]`과 같은지 검사한다. semantic fixture는 schema를 통과해야 한다.
- `covers`: `docs/contracts.md` §7 예제 이름. 테스트가 §7 목록 전체가 덮이는지 확인한다.
- scenario(`kind: "scenario"`): `security-event-batch`(records: `first_received_at` + `event`), `anomaly-detection-batch`(detections), `response-lifecycle`(command + results). valid scenario의 `expect`는 요약 값과 비교한다. scenario 안의 문서도 개별 schema·의미 검사를 먼저 통과해야 한다.

## revision 고정(MANIFEST.json)

- `files`: `contracts/<name>/<ver>/` 아래 모든 JSON(schema·fixture·index)의 sha256.
- `schemas`: schema_version → 경로·sha256.
- `revision`: 경로 오름차순 `"<경로> <sha256>\n"` 연결의 sha256. consumer는 이 값과 원본 커밋 ref를 배포 snapshot에 기록한다.
- `contracts/.gitattributes`가 JSON을 LF로 고정해 플랫폼별 hash 차이를 막는다.
- schema나 fixture를 바꾸면 `tools/hash.py --write`로 갱신하고 같은 커밋에 포함한다. `ManifestTest`가 불일치를 실패로 보고한다.

## 버전 올리는 절차

1. **호환 변경**(설명·`$comment`·fixture 추가 등, 기존 valid 문서가 계속 valid이고 invalid가 계속 invalid): 같은 디렉터리에서 수정 → 테스트 → `hash.py --write` → revision이 바뀌므로 consumer가 새 revision을 pin한다.
2. **호환 불가 변경**(필드 추가·삭제, enum 축소, 규칙 강화, 형식 변경, detection_id 입력 변경): 새 디렉터리(`security-event/v3/` 등)와 새 `schema_version`(`security-event/3.0`)을 만든다. 기존 버전 파일은 수정하지 않는다. `validate.py`의 `SCHEMA_FILES`·`CONTRACT_DIRS`, `hash.py`의 `CONTRACT_DIRS`·`SCHEMAS`를 갱신한다. ES index 이름의 버전도 함께 바꾼다.
3. enum 값 추가는 기존 consumer가 거부하게 되므로 호환 불가로 취급한다.
4. owner 문서(`docs/contracts.md`)와 이 README의 규칙 표를 같은 PR에서 갱신한다.

## 명세에서 새로 정한 것

owner 문서가 열어 둔 부분을 아래처럼 정했다. 검토 후 owner 문서에 반영하거나 새 revision으로 수정한다.

| 항목 | 결정 | 이유 |
|---|---|---|
| `token_ref` 형태 | `{key, key_version}` object 또는 null | 목적별 HMAC key version을 함께 기록. **`contracts.md` §2 합성 예제의 `"token_ref": "demo-jti"`(문자열)는 이 schema에서 거부되므로 예제 갱신 필요** |
| `operation.resource_key`, `network.ip_key` | `{key, key_version}` object 또는 null | 같은 이유. §2 예제의 `resource_key: null`은 그대로 유효 |
| 추가 reason | TOKEN_MISSING, MALFORMED(bearer 없음·파싱 불가, 서명 실패와 구분), INVALID_CREDENTIALS(Auth 로그인 실패), ROLE_MISSING(RBAC), REUSE_DETECTED(TOKEN_REUSE·RT family), STATE_UNAVAILABLE(발급대장·상태 저장소 장애 fail-closed) | §2 예시 목록만으로는 로그인 실패·토큰 누락·RBAC 거부·RT 재사용·저장소 장애를 표현할 수 없음 |
| `ACCESS_DECISION.outcome` | 접근 결정 결과(ALLOW→SUCCEEDED). 업무 commit/rollback은 같은 request_id의 BUSINESS_RESULT | "인증 성공과 업무 commit을 구분" |
| edge `outcome` | 항상 UNKNOWN | edge는 업무 결과를 알 수 없음. 최종 status는 `http`에 있음 |
| `sensitivity` | PUBLIC / STANDARD / SENSITIVE / UNCLASSIFIED | §2 예제의 SENSITIVE 외 값이 없어 최소 집합 정의 |
| `network.ip_class` | CGNAT_KR / CLOUD / ENTERPRISE_KNOWN / THREAT_FEED(`ip-classification/asn-map.yaml` 범주) + PRIVATE(로컬 lab) + UNKNOWN | 기존 분류 범주 재사용 |
| `network.source`·`trusted` | EDGE_SOCKET, TRUSTED_PROXY_CHAIN → trusted=true / UNVERIFIED_HEADER, FIXTURE → false | FIXTURE는 실제 이동 증거가 아님 |
| `client_context` | browser_family, os_family, device_class, source, trusted | "제한된 UA 파생 속성·관측 출처/신뢰" |
| `http.observation` | BACKEND_RESULT / EDGE_FINAL | §2 예제 값 + edge 최종 관측 |
| 시각·ID 표기 | UTC `Z`, 소수 초 ≤ 6자리, UUID 소문자 | ID·시각 수렴, Python 3.9/Java 파싱 공통 범위 |
| first_received_at | security-event 필드가 아님(collector metadata) | §3 "collector metadata에 기록". `IMPLEMENTATION_AGREEMENT` §8.1 표에는 이벤트 필드군으로 적혀 있어 충돌 → owner 확인 필요 |
| anomaly `schema_version` | `anomaly-detection/1.0` | ML 문서는 `anomaly-detection/1`로 표기. security-event와 같은 major.minor 형식으로 통일 |
| anomaly 추가 필드 | environment, data_origin(§4 필수 의미), event_ref, status_reason, threshold_operator | ID 충돌 방지, 품질 원인 구분, score↔flag 일치 검사 |
| anomaly score 방향 | 클수록 더 이상함 | 비교 연산만 GT/GTE로 기록 |
| response 추가 필드 | 명령 `schema_version`, 결과 `schema_version`·`mode`·`recorded_at`·`observed_state_version` | 멱등·stale·dry-run을 결과만으로 감사 |
| `evidence_ref` 형식 | `anomaly-detection:<detection_id>` 또는 `incident:<incident_id>` | 명세에 형식 없음 |
| `target_type` (명령) | SUBJECT / SESSION / IP | RATE_LIMIT의 IP 대상 가능성("공유망 피해") |

## 알려진 한계

- **원문 비밀 탐지는 패턴 기반이다.** 모든 자유 문자열에 JWS/JWE compact(`eyJ<base64url>.<base64url>.`)와 `Bearer ` 부정 패턴을 적용하고, 가명 key는 `[A-Za-z0-9_-]`만 허용해 `.`·`=`·공백이 들어가지 못하게 했다. 그러나 opaque refresh token, session cookie 값, password, HMAC 키처럼 base64url 문자만으로 된 비밀은 가명 key와 구별할 수 없다. 가명화는 producer의 HMAC adapter가 보장해야 하며 schema가 증명하지 않는다.
- 가명화는 익명화가 아니다. key_version만 기록하며 키 자체는 계약 밖이다.
- `route_template`이 실제 template인지(원시 ID가 경로에 섞였는지)는 검사하지 못한다. query·fragment·`=`·`;`·공백만 금지한다.
- UA 파생 값(`browser_family`/`os_family`)은 짧은 토큰 문자 집합으로 제한해 UA 원문을 거부하지만, 32자 이하 임의 토큰은 통과한다.
- draft 2020-12에서 `format`은 기본적으로 annotation이다. 이 도구는 `FormatChecker`를 켜고(`rfc3339-validator` 필요) 활성 여부를 테스트한다. 다른 validator는 format 검사를 켜야 `2026-02-30` 같은 달력 오류를 거부한다(패턴은 그 밖의 형식을 강제한다).
- 정규식은 ECMA-262/Java/Python 공통 부분(`[0-9]`, `[.]`, 문자 클래스, 앵커)만 사용했다. `\d`·`\w`·inline flag는 쓰지 않았다.
- 여러 문서 사이 의미 규칙은 Python `validate.py`에만 구현돼 있다. **Java 쪽 동일 검증은 아직 실행하지 않았다.** Java consumer는 같은 revision의 fixture/index로 같은 결과를 내는지 별도로 확인해야 한다.
- first_received_at은 collector metadata이며 security-event 계약 필드가 아니다. scenario 파일 형식은 테스트용이고 collector 저장 형식을 고정하지 않는다.
- RESPONSE_APPLIED 이벤트에는 command_id 필드가 없다(v2 필드 표에 없음). 명령과의 연결은 response-result가 소유한다.
