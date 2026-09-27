# build context = log-pipeline 저장소 루트 (C-02 계약 파일을 함께 넣는다)
#   docker build -f pipeline/indexer/Dockerfile -t zetty-event-indexer:<tag> .
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CONTRACTS_DIR=/app/contracts

RUN groupadd --system --gid 10001 pipeline \
 && useradd --system --uid 10001 --gid pipeline --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin pipeline

WORKDIR /app
COPY pipeline/indexer/requirements.txt /tmp/requirements.txt
RUN pip install --no-compile -r /tmp/requirements.txt && rm /tmp/requirements.txt

# 계약 snapshot: 검증기 + schema + MANIFEST(기동 시 schema sha256을 MANIFEST와 대조한다)
COPY contracts/MANIFEST.json contracts/MANIFEST.json
COPY contracts/tools/validate.py contracts/tools/validate.py
COPY contracts/security-event/v2/schema.json contracts/security-event/v2/schema.json

COPY pipeline/__init__.py pipeline/__init__.py
COPY pipeline/common/ pipeline/common/
COPY pipeline/indexer/ pipeline/indexer/
COPY pipeline/es/ pipeline/es/

USER 10001:10001
EXPOSE 9102
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9102/healthz', timeout=2)"]
ENTRYPOINT ["python", "-m", "pipeline.indexer"]
