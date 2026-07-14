# Serverless-container image: an HTTP SQL API over an embedded chDB engine
# with the dataset baked in at build time.
#
# * Two stages: the builder bakes the chDB store; the runtime stage copies
#   the store with --chown in one layer (a trailing `chown -R` would copy
#   the whole store into a second layer and double the image).
# * Baking at build time is what makes scale-to-zero workable: a cold
#   instance pulls the image and serves — no data download on boot.

FROM python:3.13-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY init_db.py .
ENV CHDB_DATA_PATH=/app/chdb-data
ARG BAKE_PARTITIONS=1
ENV BAKE_PARTITIONS=${BAKE_PARTITIONS}
RUN python init_db.py

FROM python:3.13-slim
WORKDIR /app
RUN useradd -m -u 1000 appuser
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder --chown=appuser:appuser /app/chdb-data /app/chdb-data
COPY --chown=appuser:appuser main.py agent.py ./
ENV PYTHONUNBUFFERED=1
ENV CHDB_DATA_PATH=/app/chdb-data
USER appuser
EXPOSE 8080
CMD ["python", "main.py"]
