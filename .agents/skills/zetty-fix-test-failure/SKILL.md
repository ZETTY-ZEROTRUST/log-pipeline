---
name: zetty-fix-test-failure
description: "ZETTY 테스트 실패를 원인 분석·수정·검토(최대 3회)·필요한 이미지 재빌드·실패 케이스 재검증으로 처리한다."
---

# $zetty-fix-test-failure

`$zetty-integration-test` 실패 결과: **사용자가 제공한 작업 내용**

요청과 현재 테스트 결과에서 실패 케이스·기대값·실제값을 확인합니다. 읽을 수 있는 결과가 없고 재현 명령도 결정할 수 없을 때만 필요한 정보를 질문합니다.

## 오케스트레이션 원칙

- 각 에이전트는 사용 가능한 Codex 서브에이전트 도구로 호출합니다 (상속 여부와 무관하게 범위·파일·실패 근거를 담은 self-contained 프롬프트 전달).
- 구현자↔검토자 위임 루프는 **최대 3회**. 소진하면 주 세션이 남은 결함을 직접 판단·수정·검증합니다. 해결할 수 없는 외부 조건이나 사용자 결정만 미해결로 보고하며 독립 작업은 계속합니다.
- 설치된 `devops` 역할의 재빌드와 `$zetty-integration-test` 재실행은 루프가 PASS로 종료된 후에만 수행.

## 사용 가능한 에이전트

| 이름 | 역할 |
|---|---|
| 대상 구현자(`java-spring-coder` 또는 `python-pipeline-coder`) | 실패 원인 분석 + 코드 수정 |
| `codex-reviewer` | 수정 코드 독립 검토 |
| `devops` | 필요한 로컬 Docker 이미지 재빌드 |

설치된 `.codex/agents/`에서 역할을 확인합니다. 없는 역할·위임 도구는 주 세션이 해당 단계를 수행하고 독립 검토 미실행을 명시합니다. Compose 실패는 `devops`, Java는 `java-spring-coder`, Python은 `python-pipeline-coder`가 구현 담당입니다.

---

## Step 1 — 실패 분석 및 수정 (Loop · 최대 3회)

**PASS 조건**: `codex-reviewer`가 BLOCKING finding을 보고하지 않음.

### Iteration 시작

**1-A. 대상 구현자(`java-spring-coder` 또는 `python-pipeline-coder`) 호출**

프롬프트에 포함할 것:
- `사용자가 제공한 작업 내용`의 실패 상세 전문 (테스트 번호, 기대 HTTP 코드, 실제 HTTP 코드, 응답 바디)
- "integration test 실패를 수정하는 것이 목표다. 실패 원인을 코드에서 찾아 수정하고 관련 단위 테스트를 실행하라."
- "수정 범위는 실패 증상에 한정한다. 무관한 코드를 건드리지 말 것."
- 프로젝트 루트: `git rev-parse --show-toplevel`로 확인한 값 (홈 경로를 하드코딩하지 말 것)
- 모듈 구조: `대상 앱의 기존 패키지와 계층`

**1-B. `codex-reviewer` 호출** (1-A 완료 후)

프롬프트에 포함할 것:
- 원본 실패 상세 (`사용자가 제공한 작업 내용`)
- 대상 구현자(`java-spring-coder` 또는 `python-pipeline-coder`)가 변경한 파일 목록과 변경 내용
- "변경이 실패 원인을 올바르게 수정했는지, 대상 레포의 실제 인증·이벤트·계층 계약을 위반하지 않는지 검토하라."
- 참고 파일: 실패한 테스트와 관련 실제 구현 계약

**1-C. 루프 판정**

- reviewer가 **BLOCKING 없음** → PASS, Step 2로
- reviewer가 **BLOCKING 있음**:
  - iteration < 3 → 대상 구현자(`java-spring-coder` 또는 `python-pipeline-coder`)를 BLOCKING findings와 함께 재호출 → 1-B 반복
  - iteration = 3 → 같은 위임 루프를 종료하고 주 세션이 근거를 판단합니다. 직접 해결 가능한 결함을 처리하고 실제로 막힌 부분만 아래 형식으로 보고합니다

---

## Step 2 — 필요한 이미지 재빌드

수정된 앱/consumer의 실제 Dockerfile과 Compose build context를 읽고 로컬 테스트 이미지만 재빌드한다. 테스트가 이미지를 사용하지 않으면 이 단계를 생략한다. 레지스트리에 push하거나 기존 stack을 교체하지 않는다. 빌드 실패가 승인된 수정 범위에서 해결 가능하면 처리하고, 외부 조건으로 불가능한 부분만 진단과 함께 미실행으로 남긴다.

## Step 3 — 통합 테스트 재실행

선택한 ZETTY 테스트 케이스 기준으로 **원래 실패한 시나리오만** 재실행합니다.
변경으로 영향 범위가 넓어졌다면 관련 회귀 검증도 수행합니다. 명시적으로 일부 실행만 요청된 경우에는 그 범위를 존중합니다.

재실행 결과:
- **PASS** → Step 4(최종 보고)로
- **FAIL** → 남은 3회 한도 안에서 실패 원인을 반영해 수정·검토·재검증합니다. 한도 소진 시 새 루프를 시작하지 않고 주 세션이 처리합니다. 해결 불가능한 조건만 민감값을 제거한 진단으로 보고합니다

---

## Step 4 — 최종 보고

```
[fix-test-failure] 완료

## 원인 및 수정
- 실패 케이스: T-NN <테스트명>
- 원인: <java-spring-coder가 분석한 근본 원인>
- 수정 파일:
  - path/to/File.java — <변경 요약>

## 루프 통계
- Loop 반복: X/3회
- 최종 reviewer 판정: PASS

## 재빌드
- 이미지: <해당 앱의 로컬 검증 이미지>
- 결과: SUCCESS

## 테스트 재실행
- 대상: T-NN
- 결과: PASS (HTTP <code>)

## 다음 액션
- 실행 결과와 남은 검증 확인; 커밋은 요청된 경우만
- 전체 통합 테스트 재실행: $zetty-integration-test
```

---

## 에스컬레이션 형식

```
[fix-test-failure] 중단 — Step <N>

## 상황
<무엇이 막혔는지>

## 루프 기록
- Iteration 1: <수정 내용 요약> / reviewer: <BLOCKING 내용>
- Iteration 2: <수정 내용 요약> / reviewer: <BLOCKING 내용>
- Iteration 3: <수정 내용 요약> / reviewer: <BLOCKING 내용>

## 옵션
1. 주 세션에서 해결할 수 있는 후속 작업
2. 필수 사용자 결정 또는 외부 조건
3. 독립 작업의 진행 결과
```
## ZETTY 실행 제한
요청에 케이스·범위가 명시되면 다시 묻지 않는다. 불명확할 때만 필요한 범위를 확인한다. local allowlist·모의 계정·요청 수·기간·종료 조건을 확인한다. 자신이 만든 일회용 자원만 정리한다. JWT/쿠키/비밀번호/키/업무 본문은 실패 보고에서 제거한다.

`$zetty-integration-test` 또는 `$zetty-build-change`가 현재 clone에 없으면 실제 owner의 검증 명령과 동일한 설계·구현·검토 단계를 주 세션에서 수행합니다. 다른 스킬이 실행됐다고 주장하지 않습니다.

## 현재 구현과 구성의 범위
현재 ZETTY DB 구현은 MySQL이며 PostgreSQL은 향후 전환 방향이다. 스킬·에이전트 구성만 요청된 작업에서는 실행 코드·SQL·Compose·의존성·데이터를 변경하지 않는다. 위 절차의 구현·실행 단계는 해당 작업이 실제 요청된 경우에만 적용한다. 경로는 ZETTY 워크스페이스 기준이며 개별 clone에서는 자기 레포의 README와 실제 코드를 먼저 읽고 없는 형제 레포의 교차 검증만 미실행으로 남긴다.
