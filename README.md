# ZETTY log-pipeline

현재 로컬 Compose에서 이 저장소의 상시 실행 프로세스는 **outbox-relay**와 **event-indexer**다. Auth/API가 만든 `security-event/2.0`을 MySQL Outbox에서 Redis Streams로 전달하고, 계약 검증 후 Elasticsearch에 색인한다. [Compose 구성](../zero-trust-architecture/compose/README.md)과 [전달·복구 계약](pipeline/README.md)을 함께 본다.

```text
Auth/API → MySQL security_event_outbox
         → outbox-relay → Redis Streams (redis-events)
         → event-indexer → Elasticsearch
                          ↘ MySQL receipt
```

## 현재 경로

| 위치 | 역할 |
|---|---|
| [contracts](contracts/README.md) | C-02 JSON Schema·fixture·검증기·MANIFEST revision |
| [pipeline/relay](pipeline/relay/) | Outbox lease·commit 후 Stream XADD·발행 상태 기록 |
| [pipeline/indexer](pipeline/indexer/) | consumer group·계약 검사·DLQ·ES bulk 항목별 처리 |
| [pipeline/es/security-events-v2-template.json](pipeline/es/security-events-v2-template.json) | `zetty-security-events-v2-YYYY.MM.DD` strict mapping |
| [pipeline/recovery](pipeline/recovery/) | Outbox replay |
| [pipeline](pipeline/README.md) | 실행·지표·장애·복구 세부 계약 |

전달 보장은 **at-least-once**다. ES의 `_id=event_id`와 MySQL receipt로 재전달을 멱등 처리한다. indexer는 성공한 bulk 항목만 receipt 기록 후 ACK하며 실패 항목은 재시도한다. 계약 위반 payload는 ES에 넣지 않고 이유만 DLQ에 남긴다.

## 탐지·실험 상태

2026-11-22 사용자 요청으로 직접 ML 학습·튜닝·모델 평가 코드와 노트북·모델 파일을 삭제하고 과거 실험 보고서·캡처·평가 상태도 제거했다. 기존 IF 탐지기와 학습 모델에 종속된 `pipeline/uba_analyzer`도 없다. 다음 작업은 [현재 네 시나리오](../attack-simulation/README.md)의 관측을 행동 창과 당시 업무 맥락으로 가공해 새 uba-analyzer에 전달하는 것이다. Jev 평가·보고서 작성과 Slack 알림은 목표 구조이며, 실제 API 연동·탐지 성능·전송은 아직 검증하지 않았다.

- 기존 `pipeline/detector/detect.py`와 학습 의존성은 삭제했다. 상시 Compose의 relay·indexer에는 학습 모듈 의존성이 없다.
- [pipeline/detector/anomaly_incident.py](pipeline/detector/anomaly_incident.py)는 사건 묶기·보고 형식 코드다. 외부 Slack 전송을 현재 상시 실행 경로로 간주하지 않는다.

과거 nginx PEP → Filebeat → ES ingest/`filebeat-*` 경로와 AWS EC2 배포 workflow는 현재 실행하지 않으며 후속 정리 커밋에서 제거한다. 현재 nginx `access_log`는 꺼져 있으며 이벤트 신원·인가 결론은 검증을 수행한 Auth/API에서 만든다.

## 기록과 검증

- [P-01 계약](docs/star/P-01-c02-contracts.md), [P-02 Outbox 전달](docs/star/P-02-outbox-relay-indexer.md), [P-03 Streams trim](docs/star/P-03-stream-trim-oom.md)는 해당 시점의 설계·명령·측정 조건과 결과를 기록한 STAR 자료다. 현재 기동 상태와 구분해 읽는다.
- 계약 변경은 [MANIFEST](contracts/MANIFEST.json)의 schema/fixture hash와 backend 테스트 리소스 revision을 함께 확인한다.
- 실행·복구 시험 절차는 [pipeline 문서](pipeline/README.md)의 명령과 조건을 따른다.
