---
name: zetty-event-contract-guard
description: "ZETTY 보안 이벤트·업무 맥락·탐지 결과·대응 명령의 schema와 Outbox/Streams/ES 전달·재처리를 변경하거나 검증할 때 사용한다."
---

# 신뢰 이벤트와 전달 계약

현재 세션의 최신 사용자 지시와 log-pipeline/contracts/README.md, log-pipeline/contracts/MANIFEST.json, log-pipeline/pipeline/README.md 및 변경 producer/consumer를 읽는다. 현재 세션의 사용자 지시와 실제 owner 문서를 따른다. 경로는 루트 기준이다.

## 의미와 호환성

- Auth/API에서 검증이 끝난 신원만 actor로 기록한다. edge·미검증 JWT·클라이언트 header로 신원을 확정하지 않는다.
- 새 업무 범위·승인·담당 관계에는 서버 출처와 적용 시점이 필요하다. 현재 schema에 없는 필드가 이미 수집된다고 가정하지 않는다.
- 변경할 schema/fixture·MANIFEST revision과 producer/consumer pin을 함께 확인한다. enum·null 의미·event_time·index mapping을 독립 복제하지 않는다.
- bytes 등 미관측은 null이다. 0·정상 score로 채우지 않는다. 품질 누락과 행동 위험 결과를 분리한다.

## 내구성과 정보 최소화

- 업무 성공은 동일 transaction Outbox, 거부·실패·rollback·read-only 감사는 별도 경계다.
- Outbox 원본 → Redis Streams → ES의 at-least-once와 멱등 적재를 유지한다. event_id와 event-time index는 재처리에도 고정한다.
- ES bulk 항목별 성공과 영속 receipt 이후 해당 항목만 ACK한다. pending 회수·DLQ·retry 한도·Redis 유실 후 원본 replay를 확인한다.
- token·Cookie·password·credential·키·업무 본문·원문 URL query를 기록하지 않는다. actor 가명과 route template, 허용된 집계로 추적한다.
- 탐지 consumer가 Auth DB/session Redis를 직접 수정하지 않는다. 명령과 집행 결과는 대상·TTL·버전·환경·멱등성으로 검증한다.

## 검증

owner README에 있는 실제 명령과 준비된 전용 환경으로 schema/fixture·MANIFEST·producer/consumer round trip을 확인한다. 중복·재시작·ES partial failure·늦은 이벤트·관측 누락을 변경 위험에 맞춰 검사한다. 서비스 중단/복구는 승인된 local 범위에서만 수행하며 운영 데이터·원문 비밀을 검증 출력에 넣지 않는다.

## 구조 설계 참조

[시나리오 기반 구조 설계](../../references/system-design.md)를 backend 이벤트 → log-pipeline 행동 창 → uba-analyzer/Jev → 보고서·Slack 경계를 변경할 때 읽는다. 행동 창·보고서 필드는 설계 초안이며 현재 schema에 이미 존재하는 것으로 취급하지 않는다.
