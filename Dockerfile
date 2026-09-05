FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10000 adapter \
    && useradd --no-log-init --create-home --uid 10000 --gid 10000 \
        --shell /usr/sbin/nologin adapter

WORKDIR /app
COPY pyproject.toml README.md ./
COPY targets*.yaml ./
COPY app ./app
RUN pip install --no-cache-dir . \
    && install -d -o adapter -g adapter /app/data

USER 10000:10000
EXPOSE 8787
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
