# One image, three roles. The office plans and drafts, the courier drives the
# browser, and the API serves the phone dashboard — all from the same code.
#
# Chromium only. The official Playwright image also ships Firefox and WebKit,
# and that extra download is what made `make up` sit on a 760MB layer.
FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HEADLESS=true \
    DATABASE_PATH=/data/airbnb_automate.db \
    BROWSER_USER_DATA_DIR=/data/airbnb_browser_profile

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

VOLUME ["/data"]

# Default to the all-in-one local mode; compose overrides the command per role.
CMD ["python", "manage.py", "start", "--host", "0.0.0.0", "--no-browser", "--skip-login-check"]
