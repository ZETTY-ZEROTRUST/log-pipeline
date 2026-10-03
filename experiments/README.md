# HTTP 이상 탐지 실험

ZETTY의 HTTP 토큰 악용 탐지를 위한 독립 실험 디렉토리다.
Aishani 원본 재현과 ZETTY S4 정답 평가를 분리한다. 기존 서비스 detector는 교체하지 않았다.

- [Colab 노트북](../notebooks/aishani_http_baseline.ipynb): CPU / 하드웨어 가속기 없음. 새 런타임에서 전체 실행.
- [실험 설명·평가 규칙](aishani_http_baseline/README.md)
- [구현](aishani_http_baseline/baseline.py)
- [독립 검증](aishani_http_baseline/test_baseline.py)
- [착수 계획과 실측 기록](../docs/star/HTTP-01-aishani-http-baseline.md)

실용 최소 추정: CPU 2 cores / RAM 4 GB, 권장 RAM 8 GB. 원본 2.4 MB 자료 기준이다. GPU/TPU는 필요 없다.
원본에는 공격 정답이 없으므로 이번 결과는 정확도가 아닌 재현 확인과 실행 성능이다.

## S4 행동 비교 완료

[비교 계약·CPU CLI](s4_http_comparison/README.md), [9개 확대 결과](s4_http_comparison/results/enlarged-results.md). 정상 200창 학습/200창 보정/950정상·50공격 창 평가. 모델별 결과이며 ensemble이 아니다. 12피처 SVM의 98%는 baseline이 아닌 통제된 행동 후보 결과다. 기존은 하이퍼파라미터 탐색을 하지 않았다.

로컬 실행 원본은 zetty/http-anomaly-detection에 보존했고 여기는 코드·문서·검토한 집계만 게시한다. data/runs/model을 복사하지 않았다.
