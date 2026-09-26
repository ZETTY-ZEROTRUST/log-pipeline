# Redis 없는 파일 pipeline → 탐지 — 실행 전 STAR

2026-09-27 최신 사용자 지시: Redis를 넣지 않고 pipeline 개발과 탐지까지만 연결한다. 정책/인증 집행은 범위에서 제외한다. 학습은 계속 중단한다.

## S — 문제

08은 준비된 feature replay와 Redis 실험이었다. 실제 로그를 받아 피처까지 계산하는 경로가 없었고 사용자가 Redis 제외를 요청했다. Claude의 backend/infra와 동시에 공통 runtime을 바꾸면 충돌한다.

## T — 이유와 목표

Nginx JSON 로그 파일 → 비밀/원문 식별자를 제거한 HTTP 관측 JSONL → 중복/품질 검사 → 5분 집계 → 기존 Online Shop 모델 추론 → SQLite/JSONL 탐지 결과를 연결한다. 원격 서비스·Redis·새 모델 fit 없이 실행한다. 공개 client+UA와 로컬 관측 client의 전이는 실험으로 명시하며 검증 사용자/공격 확정으로 표현하지 않는다.

## A — 구현 전 계획

- log-pipeline 별도 develop feature checkout: 엄격한 JSON 필드 allowlist, request_id 기반 ID, HMAC client/path key, UTC/bytes 검증, capture manifest와 원문 없는 오류 집계. Claude가 적용할 Nginx 형식 예제 제공. 기존 infra 파일 수정 없음.
- UBA: file manifest/순서/중복/ID 충돌 검사, 명시적 capture complete와 범위 검사, 5분 window·최소2건·누락 bytes/불완전 capture의 미평가. 학습과 동일한 HTTP 피처 함수를 사용한다.
- source 모델·threshold·hash는 기존 완료 Online Shop run을 명시한다. 무라벨이므로 기본 IF 후보는 운영 우승자가 아니라 명시적 실험 선택이다. 모델score를 공격 확률로 부르지 않는다.
- file pipeline에 Redis client/서비스/전달 명령을 제거한다. Docker는 단일 유한 추론 job이며 CPU0.5/thread1로 제한한다. 이전 Redis 검증 결과는 과거 기록으로 보존하고 현재 실행 안내와 구분한다.
- fixture만으로 정상/버스트/중복/누락/경계/모델 오류를 검사하고 결과를 기록한다. 출력 내용은 관측 이상이며 실제 공격을 했다는 뜻이 아니다.

## 예상 문제 → 개선

누락 bytes를0으로 채우는 문제 → INCOMPLETE_WINDOW; 완전성 불명 → capture.complete 기본false; 중복 재처리 → deterministic input/detection hash; raw URL/UA/IP 노출 → producer에서 HMAC/allowlist; 실제 이벤트와 공개 모델 의미 차이 → data_origin=local-http-transfer와 feature transfer version 명시.

## R — 실행 전

코드 작성 이후 RESULTS에 실제 검증·미완료 범위를 기록한다. Redis·정책·인증 연결·학습을 이번 구현에 포함하지 않는다.
