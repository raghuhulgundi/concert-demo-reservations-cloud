# Container for IBM Cloud Code Engine. Code Engine builds this from the repo with
# `ibmcloud ce app create --build-source .` (see deploy/codeengine.sh).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY server.py VERSION ./
COPY static ./static

# Run as a non-root user. The database lives in /app/data and is restored from Object Storage at startup.
RUN useradd --system --uid 1001 --home /app app \
 && mkdir -p /app/data \
 && chown -R 1001:1001 /app/data
USER 1001

EXPOSE 8080
CMD ["python3", "server.py"]
