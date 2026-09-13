# --- Stage 1: build the web UI ---------------------------------------------
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- Stage 2: python runtime -----------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY census_agent ./census_agent
RUN pip install --no-cache-dir .

COPY data ./data
COPY --from=web /web/dist ./web/dist

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8080
CMD ["uvicorn", "census_agent.app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
