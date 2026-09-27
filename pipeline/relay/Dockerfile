# build context = log-pipeline 저장소 루트
#   docker build -f pipeline/relay/Dockerfile -t zetty-outbox-relay:<tag> .
# 같은 이미지로 운영 replay 명령도 실행한다:
#   docker run --rm --entrypoint python ... zetty-outbox-relay:<tag> -m pipeline.recovery status
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 pipeline \
 && useradd --system --uid 10001 --gid pipeline --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin pipeline

WORKDIR /app
COPY pipeline/relay/requirements.txt /tmp/requirements.txt
RUN pip install --no-compile -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY pipeline/__init__.py pipeline/__init__.py
COPY pipeline/common/ pipeline/common/
COPY pipeline/relay/ pipeline/relay/
COPY pipeline/recovery/ pipeline/recovery/

USER 10001:10001
EXPOSE 9101
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9101/healthz', timeout=2)"]
ENTRYPOINT ["python", "-m", "pipeline.relay"]
