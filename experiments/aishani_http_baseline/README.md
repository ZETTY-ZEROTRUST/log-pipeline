# Aishani HTTP 이상 탐지 — 기준 버전 1

사용자 결정: **원본 방식 재현 → 측정 → 동일한 평가 조건에서 개선 → 재측정**.
이번 산출물은 첫 단계다. 기존 `pipeline/detector/detect.py`와 서비스 실행 경로에는 연결하지 않는다.

## 재현한 범위

참고: [Aishani2001/VMWare-API-Misuse-or-Abuse](https://github.com/Aishani2001/VMWare-API-Misuse-or-Abuse/tree/e8c5fc3fd149985ef73a59df3f044368d607e5d6).
원본 알고리즘을 독립 구현했으며 원본 실행 코드나 notebook을 복사하지 않았다.

| 항목 | 기준 버전 |
|---|---|
| 입력 | 공개 `access2.log`, 2,370,789 bytes, 10,000 lines |
| 집계 단위 | IP별 입력 전체 기간, 1,753 rows |
| 피처 순서 | Total Requests, Daily Mean, GET requests, Successful requests, Mean Return Size, Return Size Std, Mean Time Difference |
| 시간 | timezone 제거, IP·날짜별 첫 요청 간격 0을 평균에 포함 |
| Daily Mean | 요청이 있었던 날짜만 분모에 포함 |
| 응답 크기 | 원본과 같이 `-`를 0으로 처리, 표준편차 ddof=1, 단일 요청 std=0 |
| scaler | 전체 집계 자료에 MinMaxScaler fit |
| IF | n_estimators=200, max_features=3, contamination=.01, random_state=42, n_jobs=-1 |
| One-Class SVM | nu=.01, degree=2, kernel=rbf, gamma=scale; 요청 수 상위 1%를 제외해 학습 |
| K-Means | k=5, random_state=42, n_init=10; **모든** centroid 거리 제곱합이 **4.7 이상**이면 flag |
| 학습/판정 | 같은 자료 사용, `reference_reproduction_in_sample` |

IPv4 형태만 받는 원본 parser의 누락도 유지하되 품질 수치로 기록한다. 원본의 단일 non-match line을 제외한 9,999건이 집계되고, 누락 응답 크기 669건은 0으로 처리된다. 이는 향후 개선 대상으로 남긴 역사적 선택이며 신규 ZETTY 이벤트의 결측 처리 규칙이 아니다.

원본의 패키지 버전이 고정되어 있지 않아 모델 출력의 bit 단위 동일성은 주장하지 않는다. 현대 scikit-learn의 KMeans 기본 `n_init='auto'` 대신 과거 기본 10을 명시했다. elbow sweep·t-SNE 그래프는 이상 점수 계산에 쓰이지 않아 생략했다. 원문 URL·Referer·UA를 보관하지 않아 원본 pandas 파이프라인과 메모리 사용량은 다르다.

## Colab 실행

1. [노트북](../../notebooks/aishani_http_baseline.ipynb)을 Colab의 **파일 → 노트북 업로드**로 연다.
2. **런타임 → 런타임 유형 변경 → 하드웨어 가속기 없음(CPU)**.
3. 새 런타임에서 위에서부터 전체 실행한다. 라이브러리 버전 충돌 시 설치 후 런타임을 다시 시작한다.
4. 공개 log와 reference CSV를 고정 commit URL에서 받고 SHA-256을 검증한다. 인증이나 Drive 연결은 필요 없다.
5. 독립 fixture 검사 및 원본 CSV 대조 후, **각기 새 프로세스에서 3회** 측정한다.
6. 마지막 셀에서 report·익명 join key를 쓰는 prediction·모델을 ZIP으로 다운로드한다. 모델을 다시 로드할 때는 신뢰한 자기 산출물과 report의 checksum만 사용한다.

**실용 최소 추정:** CPU 2 cores / RAM 4 GB. RAM 8 GB이면 여유가 있다. GPU/TPU는 필요 없다.
이 추정은 2.4 MB 원본 자료와 아래의 로컬 실측을 기준으로 한다. 대용량 로그와 OCSVM 규모 확장에 적용되는 보장이 아니다. Colab 무료 CPU의 표준 메모리로 시작하고 보고서의 실제 peak RSS를 확인한다. 무료 자원 용량은 [공식 FAQ](https://research.google.com/colaboratory/faq.html)에 따라 고정 보장되지 않는다.

## 로컬 실행

Python 3.12와 `requirements.txt`의 고정 의존성을 사용한다. 준비된 venv가 있으면 재설치하지 않는다.

```bash
python -m unittest discover -s experiments/aishani_http_baseline -v
python experiments/aishani_http_baseline/baseline.py \
  --log /path/to/public/access2.log \
  --reference-csv /path/to/public/finaldata.csv \
  --output experiments/aishani_http_baseline/runs/reference-01
```

`--reference-csv`는 고정 원본 log에만 적용할 수 있다. 출력 폴더가 있으면 중단하므로 후속 측정으로 이전 실험을 덮어쓰지 않는다. `--models iforest`로 IF만 실행할 수도 있다. 결과는 `report.json`, `predictions.csv`, `baseline.joblib`이다. `runs/`·`data/`·model은 gitignore 대상이다.

## 측정값 해석과 개선 비교 규칙

- label 없는 공개 자료에서는 **flag count/rate, 처리 시간, peak RSS**만 측정한다. precision·recall·F1·PR-AUC·FPR은 `null`이다. contamination=.01이 recall 99%를 의미하지 않는다.
- peak RSS는 import를 포함한 **프로세스 생애 최대 메모리**다. notebook 전체 메모리나 입력만의 증가분이 아니다. 3회 실행은 seed=42로 고정한 실행 시간 반복이며 모델 정확도의 신뢰구간이 아니다.
- [로컬 측정 요약](results/local-reference-summary.json)은 로컬 macOS 측정이다. Colab 성능으로 해석하지 않는다.
- 다음 단계는 **정상/토큰 악용 label이 있는 고정 자료와 공통 평가 단위** 확정이다. 원본 public log를 전부 정상으로 라벨링하지 않는다. 실제 credential·원문 JWT·개인 업무 데이터는 Colab에 업로드하지 않는다.
- 개선 비교에서는 같은 capture 목록, 시간순 train/calibration/test 경계, 중복 제거 규칙, feature availability, test label, seed, 환경과 반복 횟수를 고정한다. scaler는 train에서만 fit하고 threshold는 calibration으로 정한다.
- baseline의 feature와 모델 파라미터를 유지한 **heldout 평가 실행**도 별도 version으로 먼저 만든다. 이번 in-sample 결과를 개선 모델의 heldout 정확도와 비교하지 않는다. 보지 않은 test를 반복 튜닝에 사용하지 않고 새 비교는 validation을 이용한다.
- IP 전체 기간과 사용자/시간 window의 행 수·분모는 다르다. 공통 incident/capture 단위의 TP/FP/FN/TN, precision·recall·F1·PR-AUC, 정상 사용자당 시간별 오탐, 탐지 지연을 함께 정의한 후 비교한다. class prevalence도 기록한다.
- 개선안은 한 번에 하나씩(사용자/시간 window, train-only scaler, interval/bytes 피처 등) 바꾸고 version·data/split/code hash·결과를 남긴다. 이번 단계에서는 개선안을 구현하지 않았다.

## 데이터 추적

| 자료 | SHA-256 |
|---|---|
| access2.log | `f15c31e905f86c7b4b6ab44aee74d0a2086dce89f010187d983edea7ef0364ef` |
| finaldata.csv | `3caacb4eddff809ecb5d9df606431534441f191e8287f831274074089725f793` |

입력 label: 제공되지 않음. source는 공개 repository의 고정 commit이다. 원본 tree에서 명시적 LICENSE 파일을 확인하지 못했으며 데이터 원본은 프로젝트에 재배포하지 않는다. 원문 IP를 결과에 쓰지 않는 SHA-256 join key는 공개 데이터 대조용이며 실사용자 개인정보의 익명성 보장이 아니다.
