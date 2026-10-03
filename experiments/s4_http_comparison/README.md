# S4 HTTP 탐지 비교

공개 Aishani 로그의 원본 재현은 [기존 실험](../aishani_http_baseline/README.md)에 남긴다.
이 실험은 ZETTY 로컬 시나리오의 **정답이 있는 actor별 60초 창**을 평가한다.
원본 IP별 전체기간·동일자료 학습/판정과 집계 단위가 다르므로 원본 재현이 아닌 adaptation이다.

## 비교 방법

| 사전 선언 방법 | 입력 | 학습 | 임계값 |
|---|---|---|---|
| reference_adapted | 원본에 대응하는 7개 피처 | 정상 train; MinMax도 train만 fit | IF/SVM 0, KMeans 전체 중심 제곱거리 합 4.7 |
| reference_calibrated | 같은 7개 피처 | 위와 동일 | 정상 calibration의 상위 99% 분위수 |
| improved_behavior | 요청 수·간격·URI 범위·응답 바이트·상태·method·초별 burst, 12피처 | 정상 train; MinMax도 train만 fit | 정상 calibration의 상위 99% 분위수 |

모든 방법은 같은 창·train·calibration·test를 사용한다. IF는 200 trees/max_features=3/seed42,
SVM은 RBF/nu=.01/gamma=scale, KMeans는 5 centers/n_init10/seed42다.
원본에 맞춰 reference SVM만 train 요청 수 상위 1%를 제외한다.
improved KMeans는 가장 가까운 중심의 제곱거리를 사용한다.

reference_calibrated는 임계값 보정 효과를 분리한다.
improved_behavior는 피처와 SVM 학습 표본, KMeans 점수의 변화도 포함하므로
성능 변화가 특정 피처 하나 때문이라고 주장할 수 없다.
test로 모델·임계값을 선택하지 않는다.

## 데이터 계약

생성 담당 경로: attack-simulation/datasets/s4_http.
데이터 경로: attack-simulation/results/s4-http/<run_id>.

- features.csv: window_id(연결 전용), 12개 행동 피처 + GET_count/successful_2xx_count.
- labels.csv: window_id,label,split,profile,cohort_id,window_start_utc,window_end_utc.
- split은 정상-only train → 정상-only calibration → 정상/공격 test, 전역 시간 중첩 금지.
- 원본 대응 일평균은 단일 60초 창의 요청 수와 같다. 중복 피처를 원본 대응에 보존한다.
- 요청 간격 평균은 첫 0을 포함; 응답 바이트 표준편차는 sample ddof=1, singleton=0.
- body bytes는 클라이언트 실측값. backend의 null 필드를 임의로 채운 값이 아니다.
- label/split/profile/cohort/ID/사용자/IP/시각 문자열은 모델 입력에서 제외한다.
- token/cookie/password/JWT/키/업무 응답 본문은 이 디렉토리에 넣지 않는다.

## CPU 실행

프로젝트 root의 기존 Python 3.12 venv를 사용하며 새 운영 의존성을 설치하지 않는다.

~~~bash
cd /Users/jjyj2302/zetty/log-pipeline/experiments/s4_http_comparison
../../../.venv/bin/python -m unittest -v test_comparison
../../../.venv/bin/python compare.py \
  --dataset ../../../attack-simulation/results/s4-http/<run_id> \
  --output runs/<new_run_id> --threads 2 --target-fpr 0.01
~~~

출력 디렉토리를 덮어쓰지 않는다. report.json에 split 분모, 해시, 환경/버전,
혼동행렬, precision/recall/F1/FPR, **PR-AUC 정의=average precision**, ROC-AUC,
임계값, 시간, process peak RSS를 저장한다.
predictions.csv는 test창별 점수·flag, models.joblib은 여기서 생성한 로컬 모델이다.
외부 pickle/joblib을 로드하지 않는다. 서비스 연결이나 자동 대응은 실행하지 않는다.

## 결과 해석

초기 pilot은 정상 train 60창, calibration 40창, test 100창(정상95/공격5)을 목표로 한다.
요청 건수와 평가 창 수는 서로 다른 분모다.

- 공격 5창에서 recall은 20% 간격으로 바뀐다.
- 정상 calibration 40창으로 1% 오탐률을 안정적으로 추정하기 어렵다.
  method=higher 분위수 + strict score > threshold는 경계의 동점 오탐을 피한다.
- 1%는 보정 목표이며 최종 test FPR을 보장하지 않는다.
- Wilson 95% 구간은 독립창 가정에 따른 참고값이다. 동일 actor 반복/시간 상관은 별도 한계다.
- 공격 정답은 시나리오 생성 역할에서 나오며 모델 flag를 정답으로 사용하지 않는다.
- 높은 수치도 통제된 로컬 행동 시나리오의 결과다. 실제 사고·실운영 성능으로 일반화하지 않는다.
- 모든 방법의 결과를 공개하고, 성능이 하락하면 하락을 그대로 기록한다.

## 실제 pilot 결과

[9개 모델 전체 결과와 한계](results/README.md), [해시·버전·전체 지표](results/pilot-summary.json).
정상 train60/calibration40/test95정상·5공격의 실제 요청 5,532건으로 CPU 학습을 완료했다.
7피처 SVM은 임계값 보정만으로 FP11→0, 12피처 후보는 FP4였다. 피처 추가가 항상 개선을 뜻하지 않는다.

[Expanded actual results](results/enlarged-results.md): 53,983 requests, normal train200/calibration200, test950normal/50attack. Same 9 predefined variants and code hashes; no test-based tuning.
