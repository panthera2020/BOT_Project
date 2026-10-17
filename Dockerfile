# Multi-stage production container for Trading Bot
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project specification and dependencies
COPY pyproject.toml .

# Install Python requirements
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    httpx \
    pydantic \
    aiosqlite \
    python-telegram-bot \
    jinja2

# Copy source code and templates
COPY src/ /app/src/

# Create persistent storage mount point for SQLite database
RUN mkdir -p /data
ENV DATABASE_URL="/data/trading_bot.db"

# Expose internal container port
EXPOSE 8080

# Run FastAPI server
CMD ["uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8080"]
