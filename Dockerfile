# OpenAPI Contract Compatibility Audit API
FROM python:3.11-slim

# Do not write .pyc files and flush stdout/stderr immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/app

# Install dependencies first so the layer is cached across source changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source.
COPY app ./app

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import json,urllib.request,sys; \
sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8080/health')).get('status')=='ok' else 1)"

# Bind to 8080 inside the container; Compose maps the host port.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
