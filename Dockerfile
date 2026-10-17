# Multi-stage production container for Trading Bot
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Ensure python output is sent straight to terminal/logs without buffering
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PORT=8080

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project specification
COPY pyproject.toml README.md ./

# Install Python requirements
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    httpx \
    pydantic \
    aiosqlite \
    python-telegram-bot \
    jinja2 \
    cryptography

# Copy source code and templates
COPY src/ /app/src/

# Create persistent storage mount point for SQLite database
RUN mkdir -p /data && chmod -R 777 /data /app
ENV DATABASE_URL="/data/trading_bot.db"

# Expose internal container ports
EXPOSE 8080 80 7860

# Run FastAPI server
CMD ["sh", "-c", "uvicorn src.web.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
