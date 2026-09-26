# Redis 없는 로그 생산기

Python 표준 라이브러리만 사용한다. 학습·Redis·ES·인증 서버 호출은 없다. UBA와 공유하는 파일 계약은 `contracts/http-observation/v1/schema.json` 및 `src/zetty_log/http.py`다. C-02 SecurityEvent나 검증 사용자 이벤트를 대체하지 않는 로컬 HTTP 실험 입력이다.

Claude가 검토·적용할 Nginx log_format 예제는 `nginx/http-observation-format.conf`다. Authorization/Cookie/query/body/JWT를 기록하지 않는다. 원문 IP/UA/path는 입력 파일에 있으므로 접근을 제한하고 아래 생산기에서 HMAC으로 변환한 파일만 UBA에 준다. Nginx 설정 파일 자체는 이번 작업에서 변경하지 않았다.

```bash
# 32바이트 HMAC 키는 환경 소유자가 별도 생성/보관한다. Git에 넣지 않는다.
PYTHONPATH=src /Users/jjyj2302/zetty/.venv/bin/python -m zetty_log \
  --input /path/to/nginx.jsonl --output /path/to/new-capture \
  --key-file /path/to/private/hmac-key --key-version lab-v1 \
  --capture-start 2026-09-27T00:00:00Z --capture-end 2026-09-27T00:15:00Z \
  --complete
```

`--complete`는 수집 구간에 누락이 없음을 호출자가 확인한 경우에만 사용한다. 기본값은 incomplete다. 잘못된 행이 하나라도 있으면 complete=false로 내려간다. 지나간 시간만으로 complete를 추정하지 않는다. body_bytes=null은 관측 누락 그대로 남아 UBA의 INCOMPLETE_WINDOW가 된다.

출력은 `events.jsonl`과 checksum·구간·완전성·중복/거부 수를 담는 `capture.json`이다. 동일 request_id/내용은 한 번만 기록하고 내용 충돌은 전체 작업을 실패시킨다. 최대10만 행/행64KiB이며 기존 출력 디렉터리를 덮어쓰지 않는다.

raw 로그형식은 정확히 request_id(32자리 hex), time(시간대 포함 ISO), remote_addr, user_agent, method, uri(query 없는 path), status, body_bytes다. extra field는 허용하지 않는다. observed client는 IP+UA의 가명 묶음이며 인증된 사용자로 승격하지 않는다. HMAC key/version을 바꾸면 같은 client라도 다른 관측 신원이 된다.

UBA의 `python -m zetty_uba.lab.http_file`에 capture 디렉터리를 전달한다. 원본 ref/hash를 기록한 공유 코드 snapshot을 사용하며 두 저장소에서 독립적으로 계약을 바꾸지 않는다.
