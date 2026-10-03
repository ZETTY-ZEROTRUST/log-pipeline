# P-04 S4 HTTP 비교 실험

## 착수 계획 (2026-10-03)

- Situation: 공개 Aishani 로그에는 공격 정답이 없다. 로컬 S4 정상/토큰 악용 capture로 별도 정답 평가를 만든다.
- Task: 기존 원본 재현 산출물은 보존하고 actor별 60초 창의 독립 train/calibration/test로 세 모델을 비교한다.
- Action: experiments/s4_http_comparison을 신규 소유한다. dataset 생성은 attack-simulation/datasets/s4_http 담당이 소유한다. label/ID/profile/split은 입력에서 제외한다. train-only scaler, 정상-only 학습, calibration-only threshold, 최종 test 미사용을 강제한다.
- 비교: Aishani-like 7피처·고정 임계값 adaptation, 같은 7피처의 임계값 보정, 12 행동 피처와 nearest-center KMeans 개선. 원본 IP 전체기간/in-sample 결과와 구분한다.
- 검증: 독립 계약/누수/임계값/혼동행렬 fixture; 실제 capture 도착 후 CPU 학습·정밀도·재현율·F1·AP·ROC-AUC·FPR 및 분모/해시/버전/실행시간 보고.
- 제한: test 공격 5창 pilot은 재현율 20% 단위다. calibration 정상 40창은 1% 오탐 목표를 안정적으로 추정하기 부족하다. 운영 성능이나 실제 토큰 탈취 방지로 일반화하지 않는다.

## Result

실제 pilot 완료. Git commit/push는 요청되지 않아 수행하지 않았다.

- 독립 unittest 9/9 통과.
- 실제 데이터 5,532 요청/200창: 정상 학습60·보정40·test95정상/5공격. 생성 quality pass.
- CPU 9개 모델 fit/평가 exit0. 모든 방법은 같은 test100창을 사용했다.
- 원본 대응 SVM: TP5/FP11/FN0 → 같은7피처 임계값 보정: TP5/FP0/FN0.
- 개선12피처 SVM: TP5/FP4/FN0. 피처를 늘린 후보가 보정만 한 모델보다 나빴다.
- 원본 대응 IF TP2/FP2/FN3, 개선 IF TP5/FP6/FN0. 탐지율 증가와 오탐 증가를 함께 기록한다.
- 원본 KMeans 고정4.7은 FP95, 보정하면 FP2. 집계 단위를 바꿀 때 원본 임계값을 그대로 이식하면 안 되는 근거다.
- [모든 비교 수치·환경·한계](../../experiments/s4_http_comparison/results/README.md)
- [입력·코드·artifact 해시와 전체 지표](../../experiments/s4_http_comparison/results/pilot-summary.json)

후속: 동일 test로 재튜닝하지 않는다. 더 큰 독립 capture와 새로운 holdout을 만들고 정상 burst 분포/정상 계정 다양성을 확장한 뒤 다음 후보를 사전 선언한다. 실제 token theft 탐지/운영 성능/우월성은 이번 pilot으로 입증하지 않는다.
