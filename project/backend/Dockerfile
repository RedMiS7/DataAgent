FROM node:20-slim AS frontend

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npx vite build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
# The build context allows only manifests, semantic seeds and the app schema under data/.
COPY data/ /data/
# Evaluation cases, so python -m app.evaluation runs in the container without a mount.
COPY eval/*.json /eval/
COPY DB/DABench.csv /DB/DABench.csv
COPY --from=frontend /frontend/dist ./static

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
