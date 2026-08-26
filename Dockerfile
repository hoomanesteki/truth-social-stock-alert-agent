FROM python:3.11-slim

# curl_cffi ships manylinux wheels, so no build toolchain is needed.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py ./
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY data/lexicon/ ./data/lexicon/
COPY tests/fixtures/ ./tests/fixtures/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/agent.db \
    LOG_FILE=/data/agent.jsonl

# The store and logs live on a volume so state survives a container restart.
# Dedup and alert idempotency both depend on that file persisting, otherwise a
# restart would re-alert on every post it had already seen.
VOLUME ["/data"]

# Defaults to the offline demo, which needs no network and no credentials, so
# `docker run <image>` does something useful and verifiable out of the box.
# Override with: docker run --env-file .env <image> run
ENTRYPOINT ["python", "agent.py"]
CMD ["run", "--once", "--source", "demo"]
