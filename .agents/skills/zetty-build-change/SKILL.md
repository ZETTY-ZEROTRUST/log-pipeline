---
name: zetty-build-change
description: "ZETTY 기능·수정·리팩토링을 설계→계획 검토→구현→코드 검토→테스트→문서 순서로 수행한다. 작은 문서 수정에는 전체 체인을 강제하지 않는다."
---

# $zetty-build-change

당신은 ZETTY의 변경 작업을 **에이전트 체인으로 오케스트레이션**합니다. 사용자의 요청: **사용자가 제공한 작업 내용**

요청과 저장소에서 목적을 먼저 확인합니다. 구현을 결정할 필수 정보가 없을 때만 짧게 질문하고, 답과 무관한 작업은 계속합니다.

## 오케스트레이션 원칙 (시작 전 숙지)

- 당신은 **지휘자**입니다. 에이전트가 있는 작업은 직접 하지 말고 에이전트에게 위임하세요.
- 각 에이전트는 사용 가능한 Codex 서브에이전트 도구로 위임하며 해당 역할 TOML의 지침을 전달.
- 각 에이전트에는 작업 범위·대상 레포·소유 파일·인수 기준·이미 실행한 검증을 담은 **self-contained 프롬프트**를 전달합니다. 대화 상속 여부에 의존하지 않습니다.
- 각 단계가 끝나면 한 줄 요약 후 다음 단계 판단.
- 재검토는 정해진 횟수로 제한합니다. 캡에 도달하면 같은 위임 루프를 반복하지 않고 주 세션이 남은 결함을 판단합니다. 실제 정보/권한/외부 조건 부족만 질문 또는 미해결 항목으로 남기고 독립 작업은 계속합니다.
- 독립된 읽기 검토는 사용 가능한 Codex 위임 도구로 병렬 수행할 수 있습니다. 같은 파일의 편집 담당자는 한 명입니다.

## 사용 가능한 에이전트 (이 프로젝트 전용)

| 이름 | 역할 | 권한 |
|---|---|---|
| `zetty-architect` | 계획 수립, 분류, layer 검증 | read-only |
| `codex-reviewer` | 별도 Codex 서브에이전트에서 plan/code 독립 검토 | read-only 역할 |
| `java-spring-coder` / `python-pipeline-coder` / `devops` | 대상 Java / Python / Compose 구현 | read+write+테스트 실행 |
| `postgres-expert` | 향후 PostgreSQL 전환 설계·검토 | read-only 역할 |
| `zetty-tester` | 종합 테스트 작성 | read+write+테스트 실행 |

역할 파일은 현재 레포의 `.codex/agents/`에서 확인합니다. 없는 역할·스킬이나 위임 도구를 호출했다고 주장하지 않습니다. 주 세션이 해당 단계를 수행하거나 필요한 별도 레포에 명시적으로 작업을 전달하고, 독립 검토 여부를 보고합니다.

## 실행 단계

### Step 1 — Plan (Loop A · 재계획 최대 1회)

**PASS 조건:** codex-reviewer가 **BLOCKING** finding을 보고하지 않음.

**BLOCKING 정의 — 이것만 루프를 돌린다:**
- 대상 레포의 실제 구현 계약·계층 경계 또는 기존 테스트가 입증하는 위반
- Auth/BFF/API·producer/consumer 사이의 검증된 책임 경계 파괴
- auth·RBAC 계약 파괴 (세션/CSRF/bearer 정책 우회, authority 검사 누락)
- 검증되지 않은 schema 파괴적 변경 (데이터 손실, 앱 호환성 파괴, 필요한 전환 절차 누락)
- plan이 사용자 요청을 실제로 충족하지 못함

**NON-BLOCKING — 루프 금지:** 네이밍, 파일 배치 취향, 추가 추상화 제안, "consider ...", 테스트 커버리지 아이디어, 근거 없는 성능 추측.
→ 이런 finding으로는 **절대 재계획하지 않는다.** Step 2 coder 프롬프트에 `Reviewer notes (non-blocking)` 블록으로 그대로 첨부만 한다.

1. `zetty-architect` 호출 — `사용자가 제공한 작업 내용`를 단일 요구사항으로 전달.
2. architect의 plan을 받아 `codex-reviewer` **1회** 호출:
   - 검토 대상: architect의 plan 전체 (텍스트 그대로 첨부)
   - 검토 지시: 위 BLOCKING 목록에 해당하는 것만 `BLOCKING:` 접두사로, 나머지는 전부 `NOTE:` 접두사로 출력하라고 **명시**. 애매하면 `NOTE:`로 내리라고 명시. 참고 파일로 대상 레포 README, `backend/docs/auth-token-architecture.md`, 해당 변경의 실제 테스트와 schema/fixture/revision를 직접 읽어 대조하라고 명시.
3. 판정:
   - **BLOCKING 없음** → NOTE 목록만 챙겨서 즉시 Step 2. NOTE 때문에 architect를 다시 부르지 않는다.
   - **BLOCKING 있음 & 재계획 이력 없음** → `zetty-architect`를 plan + BLOCKING findings와 함께 **딱 1회** 재호출하여 개정 plan 요청 → `codex-reviewer` 재검토.
   - **BLOCKING 있음 & 이미 1회 재계획함** → 재계획 위임 루프를 종료하고 주 세션이 근거를 직접 판단합니다. 해결 가능한 결함은 고치고, 필수 사용자 결정이나 외부 조건 때문에 불가능한 부분만 미해결로 보고합니다. 결함을 무시한 채 완료로 처리하지 않습니다.
4. PASS 시 최종 plan과 NOTE 목록을 보존하고 다음 단계로.

### Step 2 — Implement

대상 구현자(`java-spring-coder`, `python-pipeline-coder` 또는 `devops`) 호출. 프롬프트에 포함할 것:
- 사용자 원 요청 (배경)
- Step 1의 승인된 plan 중 **Files**, **Handoff: assigned coder**, **Execution order** 섹션
- Step 1 reviewer의 `Reviewer notes (non-blocking)` 목록 — "판단해서 반영하되, 이것 때문에 plan 구조를 바꾸지는 말 것" 명시
- "실제 의존 순서(schema/contracts → 대상 service/adapter → consumer/config)로 적용하고 변경된 동작의 핵심 회귀 검증을 함께 수행하라" 명시

coder는 대상 레포의 검증 명령을 실행합니다. backend는 Java 21에서 변경 앱의 `./gradlew test bootJar`, pipeline은 README의 unit/contract 및 필요한 통합 테스트, Compose는 `config --quiet`를 사용합니다. BLOCKED 보고가 오면 주 세션이 원인을 확인하고 해결 가능한 실행환경·코드 문제를 처리합니다.

### Step 3 — Review code (Loop B · 최대 3회)

**PASS 조건:** 호출된 모든 reviewer가 BLOCKING finding을 보고하지 않음.

1. coder의 보고에서 DB 코드 변경 여부 판단:
   - `backend/api-server/src/main/resources/schema.sql` 수정
   - BFF schema, JPA entity/repository, JDBC 또는 Python SQL adapter 변경
   - DB 계정·테이블·컬럼 권한 또는 설정 변경(향후 PostgreSQL 전환이면 sequence 권한 포함)
   - 트랜잭션·잠금·receipt/replay 의미 변경
   위 중 하나라도 해당 → DB 변경 있음.

2. **독립 reviewer를 사용 가능한 Codex 위임 도구로 병렬 호출**:
   - 항상: `codex-reviewer` — coder의 변경 diff와 plan 전달, "변경된 코드가 plan과 일치하고 architecture/auth 규칙을 위반하지 않는지" 검토.
   - PostgreSQL 설계·전환이 요청된 경우: `postgres-expert` — 현재 MySQL 코드와 제안된 entity/repository/schema 매핑을 전달해 변환·권한·잠금·검증 위험을 점검. 현재 MySQL만 고치는 작업은 실제 MySQL 의미를 기준으로 검토하고 PostgreSQL 문법을 강제하지 않음.

3. reviewer 중 누구라도 BLOCKING을 보고하면:
   - **iteration < 3**: 대상 구현자(`java-spring-coder`, `python-pipeline-coder` 또는 `devops`)를 BLOCKING findings와 함께 재호출하여 수정 요청 → 재검토 (DB 변경 여부 재판단).
   - **iteration = 3**: 같은 코드 검토 위임 루프를 종료합니다. 주 세션이 남은 재현 가능한 결함을 직접 해결·검증하고, 실제 외부 조건이나 사용자 결정 때문에 해결할 수 없는 부분만 이유와 함께 남깁니다. 독립 작업은 계속합니다.

4. PASS 시 다음 단계로.

### Step 4 — Comprehensive tests

`zetty-tester` 호출. 프롬프트에 포함할 것:
- 사용자 원 요청
- Step 1 plan의 **Handoff: zetty-tester** 섹션 (커버할 케이스 목록)
- coder의 최종 보고 (변경된 파일 목록)
- "안정된 코드 위에 종합 케이스를 작성하라. coder의 smoke 테스트와 중복되는 케이스는 보강 또는 통합" 지시

tester가 추가한 테스트를 자체 실행합니다. 테스트 실패 시:
- tester가 자체 컨텍스트 안에서 **테스트 코드 수정**으로 해결 가능하면 해결.
- 프로덕션 결함이면 tester는 재현 근거를 구현자/주 세션에 반환합니다. 남은 Loop B 횟수 안에서 수정·검토·실패 케이스 재검증을 진행합니다.
- Loop B를 새로 3회 시작하지 않습니다. 소진했으면 주 세션이 직접 처리합니다. 별도의 무한 coder ↔ tester 루프는 없습니다.

### Step 5 — Docs

Step 1 plan의 **Docs to update** 섹션에 따라 대상 레포의 실제 API 문서·README와 필요 시 `backend/docs/auth-token-architecture.md`를 직접 수정. 이 단계는 에이전트 위임 없이 메인 세션이 처리 (작은 텍스트 편집).

### 최종 보고

사용자에게 다음을 보고:

```
[build-app] 완료

## 요청
<원 요청>

## 변경 요약
- 파일 변경: <N개 파일>
- 테스트 추가: <N개 테스트 + 실행 결과>
- 문서 업데이트: <목록>

## 루프 통계
- Loop A (plan review): <재계획 X/1회, BLOCKING X건 / NOTE X건>
- Loop B (code review): <Y/3 iterations>

## 다음 액션 (사용자)
- 변경된 파일 검토
- 필요 시 PR 생성
- <에이전트가 남긴 open items, 있다면>
```

## 에스컬레이션 행동

주 세션은 BLOCKED·검토 캡 도달·테스트 실패를 그대로 사용자 승인 요청으로 바꾸지 않습니다. 원인을 확인해 승인된 범위에서 해결하고 독립 작업은 계속합니다. 필수 정보가 없거나 권한/외부 상태 때문에 더 진행할 수 없는 작업만 그 이유와 지금까지의 결과를 보고합니다. 이미 승인된 동작은 반복 확인하지 않습니다.

보고 형식:
```
[build-app] 중단 — Step <N>

## 상황
<무엇이 막혔는지>

## 지금까지 작업
- Step 1: <상태>
- Step 2: <상태>
- ...

## 옵션
1. <구체적 선택지>
2. <구체적 선택지>
3. 중단
```

## Skip 가능 조건

trivial 변경 (단일 파일, 동작 변경 없음, DB 무관)은 주 세션에서 처리할 수 있습니다. 전체 계획·독립 검토·테스터 체인을 강제하지 않습니다. 위험에 비례한 확인과 문서 링크·diff 검사를 수행합니다.

## 시작 전 체크리스트

1. 요청·실제 파일·인수 기준에서 작업 범위가 충분히 확인되는가?
2. 기존 사용자 변경을 식별했는가? 파괴적 동작이 있다면 해당 범위가 실제 요청에 포함되는가?
3. 대상 레포와 명령 실행 위치가 맞는가? branch 이름을 승인된 PR 대상으로 가정하지 않았는가?

확인 후 Step 1부터 진행합니다. commit/push/배포는 요청된 범위에서만 수행합니다.
## ZETTY 저장소 경계
대상 파일의 레포별 담당자를 정한다. 공통 계약·Compose는 한 명만 편집하고 독립 검토만 병렬로 수행한다. 작은 문서/이름 변경은 주 세션에서 처리하고 전체 체인을 강제하지 않는다. 서브에이전트를 쓸 수 없으면 같은 단계로 주 세션에서 진행하되 독립 검토 미실행을 명시한다. 모델은 상위 세션 설정을 상속한다.

## 현재 구현과 구성의 범위
현재 ZETTY DB 구현은 MySQL이며 PostgreSQL은 향후 전환 방향이다. 스킬·에이전트 구성만 요청된 작업에서는 실행 코드·SQL·Compose·의존성·데이터를 변경하지 않는다. 위 절차의 구현·실행 단계는 해당 작업이 실제 요청된 경우에만 적용한다. 경로는 ZETTY 워크스페이스 기준이며 개별 clone에서는 자기 레포의 README와 실제 코드를 먼저 읽고 없는 형제 레포의 교차 검증만 미실행으로 남긴다.

## 구조 설계 참조

[시나리오 기반 구조 설계](../../references/system-design.md)를 구조 변경 계획에서는 현재 세 앱과 목표 여섯 모듈을 구분한다. 설계·지침 요청이면 해당 산출물과 검토에서 종료하고 구현·테스트 실행 체인으로 자동 확대하지 않는다.
