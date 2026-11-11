import ipaddress
import re

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "BCAcctvWala API"
APP_VERSION = "1.38.3"
MIN_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    port: int = 4000

    db_host: str
    db_port: int = 5432
    db_name: str
    db_user: str
    db_password: str
    # DB_SSL=true (default) means TLS with full certificate + host name verification (libpq's "verify-full").
    # It needs DB_SSL_CA_FILE: the RDS CA bundle (https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem),
    # and DB_HOST must be the RDS endpoint name the certificate was issued for. DB_SSL=false (no TLS at all) is for
    # a local development database only.
    db_ssl: bool = True
    db_ssl_ca_file: str = ""

    jwt_secret: str
    # How long a sign-in lasts. Every token is tied to a row in auth_sessions, so it can be ended early at any
    # time (logout, logout-all, owner revokes a staff member, password / PIN change, deactivation).
    jwt_expires_minutes: int = 60 * 24 * 30  # 30 days, mirrors the Node default

    # One-time secret that must accompany POST /auth/bootstrap-admin and /auth/bootstrap-owner (header
    # X-Setup-Token). Empty = first-run setup is disabled. Without it, whoever reached a fresh server
    # first would become the platform admin / owner.
    setup_token: str = ""

    # The interactive docs (/docs, /redoc) and the OpenAPI file (/openapi.json) describe every endpoint to anyone
    # who can reach the server, no sign-in needed. Off by default; set ENABLE_DOCS=true on a development machine.
    enable_docs: bool = False

    # Since v1.17.0 the API lives under /api/v1. The old /api/... paths are OFF by default since v1.17.1 (an unused
    # second copy of every route is more to defend). Set LEGACY_API_ENABLED=true only while an app build that still
    # calls /api/... (Android 1.25.x and older) is in use.
    legacy_api_enabled: bool = False

    # Browser origins allowed to call the API (CORS), comma-separated, e.g. "https://app.example.com". The Android app
    # does not use CORS, so the default (empty) allows no browser origin at all. "*" is refused.
    cors_allowed_origins: str = ""

    # Strict-Transport-Security max-age in seconds; 0 = don't send it. Set 31536000 once the API is served over HTTPS
    # only (browsers then refuse plain HTTP to this host for that long, so don't switch it on while testing).
    hsts_max_age: int = 0

    # ---- Who the client is (v1.17.1). Rate limits, sign-in lock-outs and the audit log are all keyed by client IP.
    # Behind a reverse proxy every request comes from the proxy's address, so the real address has to be read from a
    # header the proxy sets -- but only when the request really came from that proxy, otherwise anyone could write
    # `X-Forwarded-For: <any address>` and dodge (or aim) a lock-out. TRUSTED_PROXIES lists the proxy addresses
    # (IPs or CIDR blocks, comma-separated). The default is a proxy on this same machine (nginx -> 127.0.0.1).
    # With a proxy on another host (a load balancer, CloudFront...), list its address or private range here.
    # Requests from any other address are taken at face value: their forwarding headers are ignored.
    trusted_proxies: str = "127.0.0.1,::1"
    # Optional: a single-address header your proxy sets and overwrites itself (e.g. "CF-Connecting-IP" on Cloudflare,
    # "X-Real-IP" on nginx with `proxy_set_header X-Real-IP $remote_addr`). Empty = read X-Forwarded-For from the
    # right, skipping trusted proxies, and take the first address that is not one.
    client_ip_header: str = ""
    # How many uvicorn worker processes run (the --workers value). The per-IP request limits are counted in each
    # worker's own memory, so each worker is given 1/N of the configured limit: the total across workers then stays
    # at the configured value instead of becoming N times larger.
    api_workers: int = 1

    store_state_code: str = "29"

    # Brute-force protection for credential checks (login, login-pin, change-pin, change-password, bootstrap).
    # login_rate_limit: per client IP, shared by all of those endpoints ("N/minute", "N/hour", ...).
    #   Counted in memory per worker process, so with 2 uvicorn workers the real ceiling is about 2x.
    # After login_max_failures wrong credentials in a row for one account, that account can't sign in
    #   for login_lockout_minutes -- even with the right password / PIN.
    login_rate_limit: str = "10/minute"
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    # The lock is per (account, client IP): wrong guesses from one address never lock the real user out when
    # they sign in from another one. To still stop guessing spread over many addresses, once an account has had
    # login_account_failure_ceiling failures in the window from ALL addresses together, sign-ins from addresses
    # that never signed in to it successfully (within login_known_ip_days) are refused until the window clears.
    login_account_failure_ceiling: int = 30
    # A staff / technician PIN has only 1,000,000 possibilities, so guessing it slowly from many addresses must not
    # be allowed to go on for months: once a PIN account has had this many failures in the last 24 hours from all
    # addresses together, addresses that never signed in to it successfully are refused until the 24 hours roll
    # on (0 = off). Owner / admin passwords are not affected. 100 per day means a full PIN search takes decades.
    login_pin_daily_failure_ceiling: int = 100
    login_known_ip_days: int = 30

    # The store's time zone: audit-log date filters, "today", the automatic backup time and backup file names
    # are all in it. The server process itself is also set to it (TZ in the Dockerfile) so log lines are in IST.
    app_timezone: str = "Asia/Kolkata"

    # Automatic pre-restore safety snapshots kept in R2 (newest N).
    keep_snapshots: int = 5
    max_restore_mb: int = 500
    # Backup / restore use PostgreSQL's pg_dump and pg_restore, which must be installed on this server (postgresql-client,
    # same major version as the database or newer). Set the paths if there are several versions installed.
    pg_dump_path: str = "pg_dump"
    pg_restore_path: str = "pg_restore"
    # A backup or restore that runs longer than this is stopped (a restore then changes nothing).
    backup_timeout_seconds: int = 3600
    max_upload_mb: int = 10  # per ticket photo
    max_attachment_mb: int = 8  # a scanned / photographed supplier invoice kept with a purchase (image or PDF)
    max_logo_mb: int = 2  # the shop logo upload (it is shrunk to fit the invoice header before it is stored)
    max_profile_image_mb: int = 2  # a user's profile image upload (cropped square and shrunk before it is stored)
    max_signature_mb: int = 1  # the decoded signature image sent as base64 (shrunk and trimmed before it is stored)
    # When true, a technician can't mark a ticket attended without the customer's sign-off (a signature and / or an
    # acknowledgement) or a written reason there is none. Off by default so an installed app that doesn't send it keeps working.
    require_customer_signoff: bool = False
    # Largest ordinary (JSON) request body; photo uploads and restores have their own limits (see body_limit.py).
    max_body_kb: int = 2048

    # ---- Cloudflare R2: ticket photos, backups and safety snapshots are stored ONLY here (no local disk) ----
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str  # photos (prefix service_tickets/)
    # Optional; defaults to https://<account id>.r2.cloudflarestorage.com
    r2_endpoint_url: str = ""
    # Optional separate bucket for backups and safety snapshots (prefixes backups/, snapshots/). Empty = same bucket.
    # Keep EVERY bucket private: no custom domain and no r2.dev access. Photos are only served by this API,
    # to signed-in users who are allowed to see the ticket.
    r2_backup_bucket: str = ""
    keep_cloud_backups: int = 30  # downloaded backups kept in R2 (0 = keep all)

    # Automatic backup (v1.34.0): the API itself takes a backup into R2 every day at BACKUP_SCHEDULE_TIME, read in
    # APP_TIMEZONE (so 00:00 = midnight IST). No cron job needed. With several workers only one of them runs it.
    # A failed backup is retried BACKUP_SCHEDULE_RETRIES more times, BACKUP_SCHEDULE_RETRY_MINUTES apart.
    backup_schedule_enabled: bool = True
    backup_schedule_time: str = "00:00"  # HH:MM, 24-hour
    backup_schedule_retries: int = 3
    backup_schedule_retry_minutes: int = 10

    # OPTIONAL. The platform admin has full access, so backups are plain pg_dump files by default (restorable on any
    # server, readable with pg_restore). Set this to encrypt every backup (downloads, the copies in R2 and the pre-restore
    # snapshots) with AES-256-GCM: then only servers that know the key can open them. At least 32 characters; several
    # comma-separated keys are allowed (the first encrypts, all of them decrypt -- key rotation). KEEP A COPY OUTSIDE
    # THE SERVER: without the key an encrypted backup cannot be restored.
    backup_encryption_key: str = ""

    @field_validator("app_timezone")
    @classmethod
    def _timezone_known(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"APP_TIMEZONE {value!r} is not a time zone name (use e.g. Asia/Kolkata)")
        return value

    @field_validator("backup_schedule_time")
    @classmethod
    def _schedule_time_valid(cls, value: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value.strip()):
            raise ValueError("BACKUP_SCHEDULE_TIME must be HH:MM in 24-hour time, e.g. 00:00")
        return value.strip()

    @field_validator("backup_schedule_retries", "backup_schedule_retry_minutes")
    @classmethod
    def _schedule_numbers_sane(cls, value: int) -> int:
        if value < 0:
            raise ValueError("BACKUP_SCHEDULE_RETRIES / BACKUP_SCHEDULE_RETRY_MINUTES can't be negative")
        return value

    @property
    def backup_schedule_hm(self) -> tuple[int, int]:
        hour, minute = self.backup_schedule_time.split(":")
        return int(hour), int(minute)

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_long_enough(cls, value: str) -> str:
        if len(value.encode("utf-8")) < MIN_JWT_SECRET_LENGTH:
            raise ValueError(
                f"JWT_SECRET must be at least {MIN_JWT_SECRET_LENGTH} characters. Generate one with: "
                'python3 -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return value

    @field_validator("backup_encryption_key")
    @classmethod
    def _backup_keys_long_enough(cls, value: str) -> str:
        keys = [k.strip() for k in value.split(",") if k.strip()]
        if any(len(k.encode("utf-8")) < MIN_JWT_SECRET_LENGTH for k in keys):
            raise ValueError(
                f"Every key in BACKUP_ENCRYPTION_KEY must be {MIN_JWT_SECRET_LENGTH}+ characters (or leave it empty for no encryption). Generate one with: "
                'python3 -c "import secrets; print(secrets.token_urlsafe(48))"  -- and keep a copy outside the server'
            )
        return value

    @property
    def backup_keys(self) -> list[str]:
        """The configured keys, current one first."""
        return [k.strip() for k in self.backup_encryption_key.split(",") if k.strip()]

    @field_validator("cors_allowed_origins")
    @classmethod
    def _cors_origins_explicit(cls, value: str) -> str:
        origins = [o.strip() for o in value.split(",") if o.strip()]
        if any(o == "*" or not o.startswith(("http://", "https://")) for o in origins):
            raise ValueError("CORS_ALLOWED_ORIGINS must list full origins such as https://app.example.com (no wildcard)")
        return value

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_allowed_origins.split(",") if o.strip()]

    @field_validator("trusted_proxies")
    @classmethod
    def _trusted_proxies_valid(cls, value: str) -> str:
        for item in (v.strip() for v in value.split(",") if v.strip()):
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(f"TRUSTED_PROXIES: {item!r} is not an IP address or CIDR block")
            if network.prefixlen == 0:
                # Trusting every address would let any client choose the address it appears to come from.
                raise ValueError("TRUSTED_PROXIES must list your proxy's address, not every address (no 0.0.0.0/0 or ::/0)")
        return value

    @property
    def trusted_proxy_networks(self) -> list:
        return [ipaddress.ip_network(v.strip(), strict=False) for v in self.trusted_proxies.split(",") if v.strip()]

    @field_validator("client_ip_header")
    @classmethod
    def _client_ip_header_valid(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", value):
            raise ValueError("CLIENT_IP_HEADER must be a header name such as CF-Connecting-IP or X-Real-IP")
        return value

    @field_validator("hsts_max_age")
    @classmethod
    def _hsts_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("HSTS_MAX_AGE can't be negative (0 = off)")
        return value

    @field_validator("api_workers")
    @classmethod
    def _workers_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("API_WORKERS must be at least 1")
        return value

    @field_validator("jwt_expires_minutes")
    @classmethod
    def _positive_lifetime(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("JWT_EXPIRES_MINUTES must be greater than 0")
        return value


settings = Settings()
