FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    HOTARU_SESSION_DIR=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ffmpeg \
    nodejs \
    npm \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./

RUN uv pip install --system "goygram==0.8.29" "psutil>=6.1.0" "hatchling>=1.25" "hatch-vcs>=0.4"

COPY . .

RUN uv pip install --system --no-deps -e .

EXPOSE 8787

VOLUME ["/app/sanctuary", "/app/observatory"]

ENTRYPOINT ["python3", "-m", "hotaru"]
