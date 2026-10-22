# Multi-stage Dockerfile for mypacer_scraper
# Supports both development and production environments

# =============================================================================
# Stage 'base': Common dependencies
# =============================================================================
FROM python:3.12-slim AS base

# Avoids writing .pyc files and enables unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# pip < 26.2.2 always trusts its vendored certifi, which still holds the
# compromised "emSign ECC Root CA - G3". Upgrade from a hash-pinned wheel
# before any other install (https://discuss.python.org/t/109576).
RUN pip install --no-cache-dir "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"

# Install Python dependencies (production only)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# =============================================================================
# Stage 'dev': Development environment
# =============================================================================
FROM base AS dev

# Install development dependencies
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copy only necessary files and directories
COPY --chown=appuser:appuser core/ ./core/
COPY --chown=appuser:appuser scraper/ ./scraper/
COPY --chown=appuser:appuser tools/ ./tools/
COPY --chown=appuser:appuser populate_database.sh update_database.sh ./
COPY --chown=appuser:appuser crontab ./crontab

# Create logs directory with proper permissions BEFORE removing write access
RUN mkdir -p /app/logs/archive && \
    chown -R appuser:appuser /app/logs

# Remove write permissions except for logs, and make scripts executable
RUN chmod -R a-w /app && \
    chmod 555 populate_database.sh update_database.sh && \
    chmod -R u+w /app/logs

# Switch to non-root user
USER appuser

# Keep container running for manual interaction
# Use: docker-compose exec scraper bash
CMD ["tail", "-f", "/dev/null"]

# =============================================================================
# Stage 'prod': Production environment with Supercronic
# =============================================================================
FROM base AS prod

# Install Supercronic (cron for containers)
ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.29/supercronic-linux-amd64 \
    SUPERCRONIC=supercronic-linux-amd64 \
    SUPERCRONIC_SHA1SUM=cd48d45c4b10f3f0bfdd3a57d054cd05ac96812b

RUN wget -q "$SUPERCRONIC_URL" \
    && echo "${SUPERCRONIC_SHA1SUM}  ${SUPERCRONIC}" | sha1sum -c - \
    && chmod +x "$SUPERCRONIC" \
    && mv "$SUPERCRONIC" /usr/local/bin/supercronic

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copy only necessary files and directories
COPY --chown=appuser:appuser core/ ./core/
COPY --chown=appuser:appuser scraper/ ./scraper/
COPY --chown=appuser:appuser tools/ ./tools/
COPY --chown=appuser:appuser populate_database.sh update_database.sh ./
COPY --chown=appuser:appuser crontab ./crontab

# Create logs directory with proper permissions BEFORE removing write access
RUN mkdir -p /app/logs/archive && \
    chown -R appuser:appuser /app/logs

# Remove write permissions except for logs, and make scripts executable
RUN chmod -R a-w /app && \
    chmod 555 populate_database.sh update_database.sh && \
    chmod -R u+w /app/logs

# Switch to non-root user
USER appuser

# Run Supercronic with crontab
# This will keep the container running and execute scheduled tasks
CMD ["supercronic", "/app/crontab"]
