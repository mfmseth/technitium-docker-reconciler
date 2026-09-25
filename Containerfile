# Podman-first (Containerfile is podman build's native name for this file;
# it's plain OCI build syntax, so `docker build` works against it too).
FROM python:3.14-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY reconciler.py .

ENTRYPOINT ["python", "reconciler.py"]
