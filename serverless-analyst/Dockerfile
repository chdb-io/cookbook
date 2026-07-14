# Run the published chdb-serverless package as a scale-to-zero container.
# The app is NOT vendored here — it's `pip install chdb-serverless`; this file
# only installs it, bakes the demo dataset, and assembles the image. The same
# image runs on AWS Lambda, Google Cloud Run, and Azure Container Apps.
FROM python:3.13-slim AS builder
RUN pip install --no-cache-dir "chdb-serverless[anthropic]==0.1.0"
ENV CHDB_DATA_PATH=/app/chdb-data
ARG BAKE_PARTITIONS=1
ENV BAKE_PARTITIONS=${BAKE_PARTITIONS}
RUN python -m chdb_serverless.init_db

FROM python:3.13-slim
# The AWS Lambda Web Adapter is active only under the Lambda runtime and inert
# on Cloud Run / Container Apps, so one image serves all three.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter
WORKDIR /app
RUN useradd -m -u 1000 appuser
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder --chown=appuser:appuser /app/chdb-data /app/chdb-data
ENV PYTHONUNBUFFERED=1
ENV CHDB_STORE=local:/app/chdb-data
USER appuser
EXPOSE 8080
CMD ["python", "-m", "chdb_serverless.server"]
