# HTTP 이상 탐지 실험

ZETTY의 HTTP 토큰 악용 탐지를 위한 독립 실험 디렉토리다.
현재 단계는 Aishani 원본 알고리즘 재현과 기준 측정이며, 기존 서비스 detector와 연결하지 않는다.

- [Colab 노트북](../notebooks/aishani_http_baseline.ipynb): CPU / 하드웨어 가속기 없음. 새 런타임에서 전체 실행.
- [실험 설명·평가 규칙](aishani_http_baseline/README.md)
- [구현](aishani_http_baseline/baseline.py)
- [독립 검증](aishani_http_baseline/test_baseline.py)
- [착수 계획과 실측 기록](../docs/star/HTTP-01-aishani-http-baseline.md)

실용 최소 추정: CPU 2 cores / RAM 4 GB, 권장 RAM 8 GB. 원본 2.4 MB 자료 기준이다. GPU/TPU는 필요 없다.
원본에는 공격 정답이 없으므로 이번 결과는 정확도가 아닌 재현 확인과 실행 성능이다.
