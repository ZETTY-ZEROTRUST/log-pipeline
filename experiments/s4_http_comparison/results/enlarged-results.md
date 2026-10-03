# S4 HTTP 탐지 비교 — 확대 실측

Measurement UTC: 2026-10-03T12:04:26.063267+00:00

정상 학습200창 → 정상 임계값 보정200창 → 최종 평가1,000창(정상950·공격50). 실제 서비스 조회53,983건으로 생성한 정답 자료를 사용했다.

공격 label은 학습(fit)이나 임계값 결정에 넣지 않았다. 정상으로 생성한 train/calibration만 사용했고, 공격 정답은 test의 혼동행렬·정밀도·재현율·F1·AP·ROC-AUC 채점에만 사용했다.

이번 자료의 profile_version은 2-overlapping-normal-peaks다. 정상 burst 60~120회/분과 공격100~140회/분을 겹치게 구성했다. 파일럿(normal burst48회/분)과 데이터 분포도 다르므로 두 자료 사이의 성능 변화를 단순 표본 확대나 모델 개선 효과로 해석하지 않는다.

파일럿과 동일한 9개 사전 선언 방법·모델·피처·임계값 정책을 사용했다. dataset.py/compare.py의 SHA-256이 파일럿 실행과 일치한다. threshold 수치는 정상 calibration 자료로 다시 계산했지만 test를 사용해 정책·모델을 선택하거나 튜닝하지 않았다.

| Method | Model | TP / FP / FN / TN | Precision | Recall | F1 | FPR | AP |
|---|---|---|---|---|---|---|---|
| reference_adapted | iforest | 29 / 15 / 21 / 935 | 65.91% | 58.00% | 61.70% | 1.58% | 0.64968 |
| reference_adapted | ocsvm | 34 / 44 / 16 / 906 | 43.59% | 68.00% | 53.12% | 4.63% | 0.72243 |
| reference_adapted | kmeans | 50 / 856 / 0 / 94 | 5.52% | 100.00% | 10.46% | 90.11% | 0.69850 |
| reference_calibrated | iforest | 19 / 6 / 31 / 944 | 76.00% | 38.00% | 50.67% | 0.63% | 0.64968 |
| reference_calibrated | ocsvm | 34 / 4 / 16 / 946 | 89.47% | 68.00% | 77.27% | 0.42% | 0.72243 |
| reference_calibrated | kmeans | 34 / 10 / 16 / 940 | 77.27% | 68.00% | 72.34% | 1.05% | 0.69850 |
| improved_behavior | iforest | 28 / 3 / 22 / 947 | 90.32% | 56.00% | 69.14% | 0.32% | 0.78983 |
| improved_behavior | ocsvm | 49 / 2 / 1 / 948 | 96.08% | 98.00% | 97.03% | 0.21% | 0.99771 |
| improved_behavior | kmeans | 41 / 2 / 9 / 948 | 95.35% | 82.00% | 88.17% | 0.21% | 0.98062 |

## 이번 자료 안에서의 비교

- 같은 7피처 SVM의 임계값 보정은 오탐44→4개를 줄였지만 broad 공격16개를 놓치는 문제를 해소하지 못했다.
- 12피처 SVM 후보는 공격49/50·오탐2/950, F1=0.9703이었다. 사전 선언한 후보의 이번 자료에서의 결과이며 운영 모델 채택이나 실제 사고 탐지 성능은 아니다.
- 12피처 IF 후보는 공격28/50·오탐3/950이었다. 정밀도가 높더라도 재현율56%를 함께 기록해야 한다.
- KMeans의 원본 고정4.7은 정상856/950을 이상으로 표시했다. 보정 후10개, 12피처/nearest-center 후보에서는2개였다.
- 전 모델을 공개한다. test 결과에서 가장 좋은 모델을 고른 뒤 같은 test에서 재튜닝하지 않는다.

## 한계와 후속 평가

- 정답은 통제된 S4 시나리오의 행동 역할이다. 토큰 탈취 자체를 관측한 정답이 아니며 정상 토큰으로 재현한 악용 행동이다.
- 같은100개 모의 계정이 train/cal/test의 독립 시간 구간에 등장한다. 새로운 사용자에 대한 일반화는 검증하지 않았다.
- 같은 actor의 반복창은 상관될 수 있다. report의 Wilson 구간은 독립창 가정의 nominal 참고값이며 통계적 우월성 근거가 아니다.
- 1% FPR은 정상 calibration에서의 목표다. strict >와 method=higher 분위수, 200개 정상 표본에서는 보정 관측 FPR이0.5%였다. 최종 FPR을 보장하지 않는다.
- source/피처/label/시각/split/ID를 구분했고 label/profile/cohort/windowID는 모델 입력에서 제외했다.
- 로컬 offline 평가를 완료했으며 기존 서비스 detector의 교체·배포·자동 대응은 수행하지 않았다.
- 다음 후보를 만들 때 새로운 독립 capture/holdout을 준비한다. 이번 test를 반복해서 튜닝하지 않는다.

## 재현 근거

Pipeline wall time (excluding imports/artifact write): 0.452805 s.
Process lifetime peak RSS: 156155904 bytes. CPU/BLAS threads=2. Local macOS arm64; not a Colab benchmark.

Test request counts: normal=33660, attack=6020; attack request ratio=15.1714%. The 95:5 ratio is actor-window count.

- [Safe summary and hashes](enlarged-summary.json)
- `../runs/enlarged-20261003T1140Z/report.json` (local artifact; excluded from Git)
- [Frozen protocol](../README.md)
