# S4 HTTP 비교 — 실제 pilot 결과

측정 UTC: 2026-10-03T11:17:52.944153+00:00
데이터: attack-simulation/results/s4-http/20261003T084437211295Z.
실제 요청 5,532건, actor별 60초 창 200개. 정상 학습60·보정40·최종 평가 정상95/공격5.
모든 사전 선언 방법을 같은 평가 자료에서 실행했다. 평가 결과로 모델·임계값을 다시 선택하지 않았다.

| Method | Model | TP / FP / FN / TN | P | R | F1 | FPR | AP |
|---|---|---|---|---|---|---|---|
| reference_adapted | iforest | 2 / 2 / 3 / 93 | 50.00% | 40.00% | 44.44% | 2.11% | 0.5501 |
| reference_adapted | ocsvm | 5 / 11 / 0 / 84 | 31.25% | 100.00% | 47.62% | 11.58% | 1.0000 |
| reference_adapted | kmeans | 5 / 95 / 0 / 0 | 5.00% | 100.00% | 9.52% | 100.00% | 1.0000 |
| reference_calibrated | iforest | 2 / 0 / 3 / 95 | 100.00% | 40.00% | 57.14% | 0.00% | 0.5501 |
| reference_calibrated | ocsvm | 5 / 0 / 0 / 95 | 100.00% | 100.00% | 100.00% | 0.00% | 1.0000 |
| reference_calibrated | kmeans | 5 / 2 / 0 / 93 | 71.43% | 100.00% | 83.33% | 2.11% | 1.0000 |
| improved_behavior | iforest | 5 / 6 / 0 / 89 | 45.45% | 100.00% | 62.50% | 6.32% | 1.0000 |
| improved_behavior | ocsvm | 5 / 4 / 0 / 91 | 55.56% | 100.00% | 71.43% | 4.21% | 1.0000 |
| improved_behavior | kmeans | 5 / 4 / 0 / 91 | 55.56% | 100.00% | 71.43% | 4.21% | 1.0000 |

P=정밀도, R=재현율. AP는 average precision으로 정의한 PR-AUC이며 임계값에서의 정밀도와 다르다.

## 확인한 점

- 같은 7개 피처의 SVM은 임계값 보정만으로 오탐 11→0개, 공격 탐지 5/5를 유지했다.
- 12피처 후보 SVM은 오탐 4개였다. 피처 추가가 이번 실험에서 항상 개선으로 이어지지는 않았다.
- 원본 대응 IF는 broad·burst 공격 3개를 놓쳤다. 12피처 IF는 공격5/5를 잡았지만 정상6개를 이상으로 표시했다.
- 원본 KMeans 고정 임계값4.7을 새 집계 단위에 그대로 적용하면 전체100창이 이상으로 표시됐다. 보정 후 정상 오탐은2개였다.
- 많은 모델의 AP=1인데 임계값에서의 정밀도는 낮았다. 작은 구성 자료에서 점수의 순위와 최종 판정을 구분해야 한다.

## 실행 환경과 해석 한계

CPU/BLAS threads=2, 로컬 macOS arm64. Colab 측정이 아니다.
파이프라인 실행시간 0.360049초(모듈 import·artifact 기록 제외), process lifetime peak RSS 144982016 bytes.
실제 capture 1회·학습 1회이며 안정적인 처리량이나 통계적 우월성을 입증하지 않는다.
공격5창의 재현율은20% 단위다. 5/5를 잡았어도 독립창 가정의 nominal Wilson 95% 구간은 약56.6~100%다.
오탐0/95의 nominal FPR 구간은0~3.89%다. 동일 actor 반복창의 상관으로 이 구간도 과도한 확신을 줄 수 있다.
정상 보정40창은1% 오탐률을 정밀하게 추정하기 부족하다.
train/cal/test에 같은 모의 계정이 등장한다. 새 사용자에 대한 일반화는 평가하지 않았다.
유효 토큰으로 생성한 통제된 S4 행동을 평가했으며, 토큰 탈취 자체나 실제 사고 탐지를 입증한 결과가 아니다.

## 학습 자료에서만 확인한 피처 진단

12피처 중4xx_ratio/5xx_ratio/GET_ratio/POST_ratio가 정상 train60창에서 상수였다. 원본 대응7피처는 상수가 없었다.
성공한 GET 요청 중심의 자료에서 이4개 피처는 학습 변동을 추가하지 않는다. 오탐 원인을 확정한 것은 아니며 비교 설정은 동결했다.
[진단과 입력 해시](pilot-train-feature-diagnosis.json).

## 근거

- [전체 안전 집계·해시·버전](pilot-summary.json)
- `../runs/pilot-20261003T090000Z/report.json` (local artifact; excluded from Git)
- [사전 비교 방법](../README.md)

## Expanded independent capture

[All 9 expanded results and limitations](enlarged-results.md), [safe expanded summary](enlarged-summary.json). The normal burst distribution changed; do not interpret pilot-to-expanded differences as model improvement or sample-size effects alone.
