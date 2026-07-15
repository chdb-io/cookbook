# Image for AWS Lambda MicroVMs: the chdb-serverless analyst plus the six
# MicroVMs lifecycle hooks, with the dataset baked in at build time.
#
# * Base is the public-ECR mirror of python:3.13-slim so the MicroVM build
#   role can pull it without Docker Hub auth or rate limits.
# * The app is `pip install chdb-serverless` (the published package); only the
#   two-port hooks server (main.py) is specific to MicroVMs.
# * CMD starts main.py, which serves the app (:8080) and the six lifecycle
#   hooks (:9000) in one process — the /ready hook warms the same engine the
#   app serves from, so the snapshot captures it hot.

FROM public.ecr.aws/docker/library/python:3.13-slim AS builder
RUN pip install --no-cache-dir "chdb-serverless[anthropic]==0.1.0"
ENV CHDB_DATA_PATH=/app/chdb-data
ARG BAKE_PARTITIONS=1
ENV BAKE_PARTITIONS=${BAKE_PARTITIONS}
RUN python -m chdb_serverless.init_db

FROM public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app
RUN useradd -m -u 1000 appuser
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder --chown=appuser:appuser /app/chdb-data /app/chdb-data
COPY --chown=appuser:appuser main.py ./
ENV PYTHONUNBUFFERED=1
ENV CHDB_STORE=local:/app/chdb-data
USER appuser
# 8080: application traffic (routed by the MicroVM proxy). 9000: lifecycle hooks.
EXPOSE 8080 9000
CMD ["python", "main.py"]
