FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md ./
COPY trading_system ./trading_system

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

EXPOSE 8000 8501

CMD ["uvicorn", "trading_system.app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
