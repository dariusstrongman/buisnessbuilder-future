FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY contracts ./contracts
COPY prototype ./prototype
COPY staging_server.py ./

RUN pip install --no-cache-dir . \
    && groupadd --system businessbuilder \
    && useradd --system --gid businessbuilder --home-dir /nonexistent businessbuilder

USER businessbuilder
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]

CMD ["python", "staging_server.py"]
