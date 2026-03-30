FROM python:3.14-slim AS base

# Prevent Python from buffering stdout/stderr (important for Railway logs)
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ── System dependencies ──────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    poppler-utils \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies (cached layer) ───────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ─────────────────────────────────────────
# Copy PageIndex source (from the forked repo)
COPY pageindex/ ./pageindex/
COPY run_pageindex.py .
COPY cookbook/ ./cookbook/
COPY examples/ ./examples/ 

# Copy config if present
COPY config.yaml* ./

# Copy production server files
COPY config.py .
COPY db.py .
COPY cache.py .
COPY storage.py .
COPY models.py .
COPY server.py .

# ── Create volume mount point ────────────────────────────────
RUN mkdir -p /data/pdfs

# ── Runtime ──────────────────────────────────────────────────
ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

CMD ["python", "server.py"]
