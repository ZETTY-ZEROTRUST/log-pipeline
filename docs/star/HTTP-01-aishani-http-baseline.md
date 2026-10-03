# P-03 Aishani HTTP 이상 탐지 기준 버전 재현

- 상태: 로컬 재현·CPU 3회 측정 완료; Colab 미실행
- 작성: 2026-10-03
- 범위: 독립 오프라인 실험. 기존 detector와 서비스 연결은 변경하지 않는다.

## S — 문제 발생

HTTP 토큰 악용 탐지 모델을 개선하기 전에 비교 기준이 필요하다. 현재 detector의 사용자 집계 5개 피처와 Aishani 원본의 IP 집계 7개 피처는 서로 다르다. 원본 저장소는 정답 label 없이 같은 자료를 학습하고 판정하므로 탐지 정확도 근거가 없다.

## T — 목표

1. 원본 commit `e8c5fc3fd149985ef73a59df3f044368d607e5d6`의 피처 계산과 세 모델을 독립적으로 재현한다.
2. 공개 `access2.log`의 집계값을 원본 `finaldata.csv`와 대조한다.
3. 시간·프로세스 peak RSS·flag 수·입력 및 코드 hash·의존성·seed를 기록한다.
4. 단독 실행 가능한 CPU Colab 노트북을 제공한다.

## A — 계획

- IP 전체 기간 집계, active-day 평균, 하루 첫 간격 0 포함, 누락 바이트 0, 표본 표준편차를 그대로 유지한다.
- MinMaxScaler를 전체 자료에 fit하고 동일 자료를 판정하는 `reference_reproduction`으로 명시한다.
- IF 200 trees / max_features=3 / contamination=.01 / seed=42, OCSVM 원본 파라미터, KMeans 5 clusters / 전체 centroid 거리 제곱합 / cutoff 4.7을 유지한다.
- 원본에 의존성 lock이 없으므로 모델 출력의 bit 단위 일치를 주장하지 않는다. KMeans의 과거 기본 n_init=10을 명시하고 차이를 기록한다.
- 독립 fixture와 원본 집계 CSV 대조 후 로컬에서 3회 측정한다. Colab 실행은 별도이며 측정했다고 기록하지 않는다.
- 개선 모델은 이번 단계에서 구현하지 않는다. 공통 label 평가 자료와 평가 단위를 먼저 확정한 다음 같은 holdout에서 비교한다.
- 원본 코드의 복사 대신 알고리즘을 독립 구현한다. 원문 IP·URI·UA는 산출물에 저장하지 않는다.
- Git commit/push는 사용자 요청이 없으므로 수행하지 않는다. STAR의 사전 commit 절차는 보류하고 계획을 구현 전에 작성한다.

## R — 결과

9,999요청·1,753 IP·7피처 12,271값 일치. IF18/SVM101/K-Means29는 이상 표시이며 공격 정답 없음. 3회 pipeline median0.259600초, peak RSS144.6MiB. [안전한 집계](../../experiments/aishani_http_baseline/results/local-reference-summary.json).
