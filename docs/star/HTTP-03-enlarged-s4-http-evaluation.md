# P-05 확대 S4 HTTP 학습·평가 결과

## Situation / Task

사용자가 요청한 다량의 정상·S4 행동 데이터를 생성한 뒤 같은 사전 선언 모델로 독립 평가한다. 파일럿은 보존한다.

## Action

- 생성담당의 정상train200/calibration200/test950정상50공격, profilev2 데이터만 사용했다.
- normal-only fit, train-only MinMax, calibration-only threshold, test 정답은 채점에만 사용했다.
- 기존9개 방법과 모델을 유지하고 코드 해시가파일럿과 동일함을 검증했다.
- 실제 CPU 학습1회 exit0, 코드·자료·artifact 해시와 환경·분모·전체9모델을 기록했다.

## Result

- 실제조회53,983건/1,400 actor별60초 창; quality pass.
- 12피처 SVM 후보 TP49/FP2/FN1/TN948, 정밀도96.08%·재현율98%·F1 97.03%·FPR0.2105%.
- 7피처 보정 SVM TP34/FP4/FN16/TN946. 자료가같은이번실험안에서만 비교한다.
- 모든모델·정량결과·해시·UTC·한계: [확대결과](../../experiments/s4_http_comparison/results/enlarged-results.md), [전체집계](../../experiments/s4_http_comparison/results/enlarged-summary.json).
- 파일럿과 확대는 정상burst분포도 달라 두실험의차이를 모델개선율로표현하지 않는다.
- 반복계정창상관, 새사용자미평가, 통제된행동시나리오, 서비스 detector 미배포의한계를남겼다.
- Git 작업은 사용자 승인 후 공통 담당(root)이 관리한다. 이 학습 에이전트는 git init/remote/stage/commit/push를 수행하지 않았다.
