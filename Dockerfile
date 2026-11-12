# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

# pg_dump / pg_restore are used by the backup + restore feature. Their major version must be the SAME AS OR NEWER
# than your RDS PostgreSQL version. Override with: docker build --build-arg PG_MAJOR=18 -t bcacctvwala-api .
ARG PG_MAJOR=17

# TZ: the whole container (log lines, `date`, cron-like code) runs in Indian Standard Time. The API's own business
# logic uses APP_TIMEZONE from .env (also Asia/Kolkata by default); keep the two the same.
ENV TZ=Asia/Kolkata \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
 && ln -snf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime && echo Asia/Kolkata > /etc/timezone \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-${PG_MAJOR} \
 && curl -fsSL -o /etc/ssl/certs/rds-global-bundle.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
 && grep -q "BEGIN CERTIFICATE" /etc/ssl/certs/rds-global-bundle.pem \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --system --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 4000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4000/health', timeout=4).status == 200 else 1)"

# --no-proxy-headers: the API reads the proxy headers itself (TRUSTED_PROXIES / CLIENT_IP_HEADER), see README 2q.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 4000 --workers ${API_WORKERS:-2} --no-proxy-headers"]
