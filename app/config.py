from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://portal:portal@localhost:5432/portal"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://portal:portal@localhost:5432/portal"
    REDIS_URL: str = "redis://localhost:6379/0"
    USE_STUB_TERRAFORM: bool = True
    DEV_USER_ID: str = "dev-user-00000000"

    # URL prefix the app is mounted under when behind a reverse proxy on a subpath
    # (e.g. "/dp" for https://host/dp/). Empty = served at the root. Passed to FastAPI as
    # root_path so the generated docs/OpenAPI URLs carry the prefix.
    ROOT_PATH: str = ""

    # Which blue-green slot this process is running as ("blue" / "green"), purely informational.
    # Set via the APP_SLOT env var in docker-compose.prod.yml's app_blue/app_green services.
    # Empty = not running under blue-green (e.g. dev compose). Surfaced read-only on GET /health.
    APP_SLOT: str = ""

    # Auth
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = ""
    SESSION_TTL: int = 86400
    # Send the session cookie only over HTTPS. Default True (production runs behind TLS);
    # set False for local development over plain http://localhost.
    SESSION_COOKIE_SECURE: bool = True
    # Canonical origin the browser uses to reach this portal (scheme + host + optional port,
    # no trailing slash). Used by CSRFOriginMiddleware to reject requests from foreign origins.
    # Must match the URL in the browser's address bar (e.g. https://dp.my-domain.com).
    BASE_URL: str = "http://localhost:8000"
    # Max bcrypt hash/verify operations running at once in this process (#493). They run on a
    # dedicated thread pool off the event loop; work beyond this waits for a free slot. Unset =
    # the number of CPUs available to the process. Per process: N uvicorn workers → N × this.
    BCRYPT_MAX_CONCURRENCY: int | None = Field(None, gt=0)

    # Per-user resource quotas (defaults applied when no per-user row exists)
    DEFAULT_QUOTA_CPUS: int = 16
    DEFAULT_QUOTA_MEMORY_GB: int = 32
    DEFAULT_QUOTA_SSD_GB: int = 500
    DEFAULT_QUOTA_HDD_GB: int = 500

    # Celery provision task
    PROVISION_MAX_RETRIES: int = 3
    PROVISION_RETRY_DELAY: int = 120   # seconds
    PROVISION_RATE_LIMIT: str = "0.5/m"

    # Celery beat tasks
    ENFORCE_TTL_INTERVAL_SECONDS: int = 60       # how often to release expired bookings
    STALE_PROVISIONING_THRESHOLD_MINUTES: int = 60

    # Environments page: environments per page / per "Load more" (keyset pagination, #467).
    # Server-side only — never a query parameter, so a request can't ask for an unbounded page.
    ENVIRONMENTS_PAGE_SIZE: int = Field(50, gt=0)
    # Bookings pages: bookings per page / per "Load more" (keyset pagination, #479). Same rule.
    BOOKINGS_PAGE_SIZE: int = Field(50, gt=0)
    # Label-filtered bookings pages: bookings examined per page / per "Search older bookings"
    # (#485). The label is a substring match no index can serve in page order, so each request
    # examines at most this many bookings of the page's filter range and shows the ones whose
    # label matches. Must exceed BOOKINGS_PAGE_SIZE, or even a label matching everything would
    # never fill a page.
    BOOKINGS_LABEL_SCAN_SIZE: int = Field(200, gt=0)
    # Page row reconciliation (#497): each list section sends one request per interval naming at
    # most RECONCILE_MAX_IDS displayed rows (≤ either page size, so a request never reads more
    # than a list page); RECONCILE_SETTLED_MIN slots of each batch are kept for settled
    # (READY/FAILED) rows, so they can't be starved by in-flight ones — hence at least 1.
    RECONCILE_MAX_IDS: int = Field(50, ge=1)
    RECONCILE_SETTLED_MIN: int = Field(10, ge=1)
    # Most children an environment may have (#497): enforced on blueprint save and on order, so
    # reconciliation's bounded per-environment child read always sees every child.
    ENVIRONMENT_MAX_CHILDREN: int = Field(25, ge=1)

    # Live row updates (SSE): progress-only row-changed notifications (one per Ansible/script
    # output line) are coalesced per booking to at most one per window, plus a trailing publish
    # after a burst (#440). 0 disables coalescing. Read at worker start-up.
    SSE_PROGRESS_COALESCE_MS: int = 750
    # Progress persistence batching (#444): the first progress line after a quiet period is saved
    # at once; later lines are saved together at most once per interval, or right away once the
    # buffer reaches either flush threshold. The thresholds trigger flushes — they don't bound the
    # buffer (the 50,000-character log cap does). 0 = save every line as it arrives. Read at
    # worker start-up.
    PROGRESS_FLUSH_INTERVAL_MS: int = Field(500, ge=0)
    PROGRESS_FLUSH_MESSAGE_THRESHOLD: int = Field(50, ge=1)
    PROGRESS_FLUSH_CHAR_THRESHOLD: int = Field(16_384, ge=1)

    # Terraform / VCD — only required when USE_STUB_TERRAFORM=False
    TF_WORKSPACES_DIR: str = "/tmp/tf-workspaces"
    TF_PG_CONN_STR: str = "postgresql://portal:portal@postgres:5432/portal?sslmode=disable"
    TF_MODULE_SOURCE: str = "/app/terraform/modules/vapp_vm"
    TF_APPLY_REFRESH: bool = True
    TF_APPLY_PARALLELISM: int = 1
    VCD_URL: str = ""
    VCD_NETWORK_NAME: str = ""
    VCD_ORG: str = ""
    VCD_VDC: str = ""
    VCD_API_TOKEN: str = ""
    VCD_API_TOKENS: str = ""   # comma-separated; overrides VCD_API_TOKEN when set
    VCD_TOKEN_LOCK_TTL: int = 900   # Redis lock TTL in seconds
    VCD_TOKEN_MAX_PARALLEL: int = 4   # max concurrent provisioning jobs per token
    VCD_USER: str = ""
    VCD_PASSWORD: str = ""
    VCD_ALLOW_UNVERIFIED_SSL: bool = False

    # Post-provision VM configuration — the worker SSHes into a freshly provisioned VM to run a
    # startup script (P1.2) and Ansible roles (P2.2).
    VM_SSH_USER: str = "root"
    VM_SSH_PORT: int = 22
    VM_SSH_PRIVATE_KEY: str = ""        # PEM key path; empty → password auth with the VM password
    CONFIG_SSH_TIMEOUT: int = 300       # seconds to wait for the VM's SSH to come up (0 disables)
    CONFIG_SSH_RETRY_INTERVAL: int = 30  # seconds between SSH connect attempts while waiting
    ANSIBLE_ROLES_PATH: str = "/app/ansible/roles"  # where the worker looks up roles (admins add theirs)
    ANSIBLE_COLLECTIONS_PATH: str = "/opt/ansible/collections"  # outside /app so bind-mount doesn't shadow it
    ANSIBLE_TIMEOUT: int = 1800         # seconds before an ansible-playbook run is killed
    ANSIBLE_VERBOSITY: int = 0          # 0 = default output, 1-3 = -v / -vv / -vvv

    # Ansible role secret vars (Fernet encryption at rest)
    # Generate a key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Fail-closed: roles with non-empty secret_vars are rejected if this key is unset.
    SECRET_VARS_ENABLED: bool = True
    SECRETS_ENCRYPTION_KEY: str = ""

    @model_validator(mode="after")
    def _label_scan_exceeds_page_size(self):
        if self.BOOKINGS_LABEL_SCAN_SIZE <= self.BOOKINGS_PAGE_SIZE:
            raise ValueError(
                f"BOOKINGS_LABEL_SCAN_SIZE ({self.BOOKINGS_LABEL_SCAN_SIZE}) must be greater than "
                f"BOOKINGS_PAGE_SIZE ({self.BOOKINGS_PAGE_SIZE})"
            )
        return self

    @model_validator(mode="after")
    def _reconcile_batch_fits_a_page(self):
        page = min(self.BOOKINGS_PAGE_SIZE, self.ENVIRONMENTS_PAGE_SIZE)
        if self.RECONCILE_MAX_IDS > page:
            raise ValueError(
                f"RECONCILE_MAX_IDS ({self.RECONCILE_MAX_IDS}) must not exceed the smaller page size ({page})"
            )
        if self.RECONCILE_SETTLED_MIN >= self.RECONCILE_MAX_IDS:
            raise ValueError(
                f"RECONCILE_SETTLED_MIN ({self.RECONCILE_SETTLED_MIN}) must be less than "
                f"RECONCILE_MAX_IDS ({self.RECONCILE_MAX_IDS})"
            )
        return self


settings = Settings()
