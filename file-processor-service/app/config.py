from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Service configuration, read from environment variables (set by Terraform on Cloud Run)."""

    gcp_project: str
    gcp_region: str = "europe-west2"

    landing_bucket: str
    invalid_bucket: str
    archive_bucket: str
    staging_bucket: str
    config_bucket: str
    contracts_prefix: str = "contracts/"
    # how long a cached contract lives when nothing refreshes it (POST /contracts does)
    contract_cache_ttl_seconds: int = 300

    audit_dataset: str = "file_audit"
    job_table: str = "job_metadata"
    validation_table: str = "validation_metadata"

    max_sample_failures: int = 10
    # An IN_PROGRESS job older than this is treated as crashed and may be retried.
    in_progress_timeout_seconds: int = 660
    load_timeout_seconds: int = 480

    service_version: str = "0.1.0"
    runtime_service_account: str | None = None
    # Set by Cloud Run.
    k_service: str | None = Field(default=None, alias="K_SERVICE")
    k_revision: str | None = Field(default=None, alias="K_REVISION")


@lru_cache
def get_settings() -> Settings:
    return Settings()
