# Image for AWS Lambda MicroVMs: an HTTP SQL API over an embedded chDB engine
# with the dataset baked in at build time.
#
# * Base is the public-ECR mirror of python:3.13-slim so the MicroVM build
#   role can pull it without Docker Hub auth or rate limits.
# * Two stages: the builder bakes the chDB store; the runtime stage copies
#   the store with --chown in one layer (a trailing `chown -R` would copy
#   the whole store into a second layer and double the image).
# * CMD starts main.py, which serves the app (:8080) and the six Lambda
#   MicroVMs lifecycle hooks (:9000) in one process — the /ready hook warms
#   the same engine the app serves from, so the snapshot captures it hot.

FROM public.ecr.aws/docker/library/python:3.13-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY init_db.py .
ENV CHDB_DATA_PATH=/app/chdb-data
ARG BAKE_PARTITIONS=1
ENV BAKE_PARTITIONS=${BAKE_PARTITIONS}
RUN python init_db.py

FROM public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app
RUN useradd -m -u 1000 appuser
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder --chown=appuser:appuser /app/chdb-data /app/chdb-data
COPY --chown=appuser:appuser main.py agent.py ./
ENV PYTHONUNBUFFERED=1
ENV CHDB_DATA_PATH=/app/chdb-data
USER appuser
# 8080: application traffic (routed by the MicroVM proxy). 9000: lifecycle hooks.
EXPOSE 8080 9000
CMD ["python", "main.py"]
