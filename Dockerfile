# Backend API image for single-service deployments (e.g. Render).
# Build context: repository root. Copies backend/ sources from this repo.
FROM python:3.11.17-slim-trixie

# System dependencies: ffmpeg for media, tesseract for OCR, deno for yt-dlp JS challenges,
# and Noto fonts for multilingual caption fallback.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    tesseract-ocr \
    tesseract-ocr-eng \
    curl \
    unzip \
    fonts-noto-core \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    fontconfig \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# deno is required by yt-dlp for YouTube JS challenge solving.
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_DIR=/root/.deno
ENV PATH="/root/.deno/bin:${PATH}"

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install Python dependencies from the backend lockfile.
COPY backend/pyproject.toml backend/uv.lock* ./

# Default to CPU wheels for hosted/free deployments (CUDA wheels are very large
# and require NVIDIA GPUs). Override with --build-arg BACKEND_CPU_ONLY=false.
ARG BACKEND_CPU_ONLY=true
RUN uv venv .venv && \
    if [ "$BACKEND_CPU_ONLY" = "true" ]; then \
      uv sync --frozen --extra cpu; \
    else \
      uv sync --frozen --extra cuda; \
    fi

COPY backend/src/ ./src/
COPY backend/fonts/ ./fonts/
COPY backend/transitions/ ./transitions/

RUN mkdir -p /app/uploads /app/clips /app/logs /tmp

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Render (and similar platforms) inject PORT at runtime.
EXPOSE 8000
CMD ["sh", "-c", ".venv/bin/uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
