FROM python:3.12-slim

# System deps for Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libgbm1 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libpango-1.0-0 \
    libcups2 \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Playwright browsers installed to a shared path (not $HOME/.cache) so they
# remain accessible after we drop to a non-root user below.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (chromium) to the shared path
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

# Create non-root user and hand ownership of app + browser cache + debug dir.
# Chromium with --no-sandbox runs fine unprivileged. Debug captures (/tmp)
# are writable by any user due to 1777 permissions.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 1000 appuser \
    && mkdir -p /app/data /tmp/agent-debug \
    && chown -R appuser:appuser /app /ms-playwright /tmp/agent-debug

USER appuser

# Browser Use needs this
ENV DISPLAY=:99
ENV CHROMIUM_PATH=/usr/bin/chromium

EXPOSE 8000

# Docker HEALTHCHECK hits the unauthenticated /healthz liveness probe.
# Python stdlib urllib is used because curl is not installed in the image.
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=5); sys.exit(0 if r.status==200 else 1)" || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
