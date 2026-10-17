import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.audit import JobRecord, PriorAttempt
from app.config import Settings
from app.contracts import ContractRegistry, directory_source
from app.loader import LoadResult
from app.models import CheckRow, PushEnvelope
from app.processor import FileProcessor
from app.storage import gcs_uri

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "config" / "contracts"
LANDING = "proj-poc-landing"

TABLE_COLUMNS = {
    "supplementary_data.customer": [
        "customer_id", "first_name", "last_name", "email", "phone", "country", "signup_date", "is_active",
        "_job_id", "_source_file", "_loaded_at",
    ],
    "supplementary_data.user": [
        "user_id", "username", "email", "customer_id", "role", "created_at", "last_login_at",
        "_job_id", "_source_file", "_loaded_at",
    ],
    "supplementary_data.orders": [
        "order_id", "customer_id", "order_date", "amount", "currency", "status",
        "_job_id", "_source_file", "_loaded_at",
    ],
}  # fmt: skip


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.fail_move: Exception | None = None

    def put(self, bucket: str, name: str, data: bytes) -> None:
        self.objects[(bucket, name)] = data

    def names(self, bucket: str) -> list[str]:
        return sorted(n for b, n in self.objects if b == bucket)

    def download(self, bucket, name, dest: Path, generation=None) -> None:
        dest.write_bytes(self.objects[(bucket, name)])

    def upload(self, src: Path, bucket, name, content_type="text/csv") -> str:
        self.objects[(bucket, name)] = src.read_bytes()
        return gcs_uri(bucket, name)

    def move(self, src_bucket, name, dst_bucket, dst_name, generation=None) -> str:
        if self.fail_move:
            raise self.fail_move
        self.objects[(dst_bucket, dst_name)] = self.objects.pop((src_bucket, name))
        return gcs_uri(dst_bucket, dst_name)

    def delete(self, bucket, name) -> None:
        self.objects.pop((bucket, name), None)


class FakeAudit:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.validations: list[tuple[str, CheckRow]] = []
        self.prior: list[PriorAttempt] = []
        self.fail_insert_job: Exception | None = None

    def find_attempts(self, bucket, name, generation) -> list[PriorAttempt]:
        return self.prior

    def insert_job(self, job: JobRecord) -> None:
        if self.fail_insert_job:
            raise self.fail_insert_job
        self.jobs[job.job_id] = job.columns()

    def update_job(self, job: JobRecord, updated_at) -> None:
        job.set(updated_at=updated_at)
        self.jobs[job.job_id] = job.columns()

    def insert_validations(self, job, results, validated_at) -> None:
        self.validations.extend((job.job_id, r) for r in results)

    @property
    def job(self) -> dict:
        assert len(self.jobs) == 1, self.jobs
        return next(iter(self.jobs.values()))

    @property
    def checks(self) -> list[CheckRow]:
        return [c for _, c in self.validations]

    def failed_rules(self) -> set[str]:
        """Rule types that failed, from failed_checks labels like 'email.REGEX'."""
        return {label.rsplit(".", 1)[-1] for c in self.checks for label in c.failed_checks}


class FakeLoader:
    def __init__(self) -> None:
        self.loads: list[tuple[str, str, str]] = []
        self.fail: Exception | None = None
        self.output_rows: int | None = None  # None = count rows in the staged file

    def table_id(self, target_table: str) -> str:
        return f"proj.{target_table}"

    def table_columns(self, target_table: str) -> list[str]:
        return TABLE_COLUMNS[target_table]

    def load(self, source_uri, target_table, write_disposition) -> LoadResult:
        if self.fail:
            raise self.fail
        self.loads.append((source_uri, target_table, write_disposition))
        return LoadResult("bq_job_1", self.output_rows if self.output_rows is not None else self._staged_rows)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        gcp_project="proj",
        landing_bucket=LANDING,
        invalid_bucket="proj-poc-invalid",
        archive_bucket="proj-poc-archive",
        staging_bucket="proj-poc-staging",
        config_bucket="proj-poc-config",
    )


@pytest.fixture
def registry() -> ContractRegistry:
    return ContractRegistry(directory_source(CONTRACTS_DIR))


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def audit() -> FakeAudit:
    return FakeAudit()


@pytest.fixture
def loader(storage: FakeStorage) -> FakeLoader:
    fake = FakeLoader()
    original_load = fake.load

    def load(source_uri, target_table, write_disposition):
        bucket, name = source_uri.removeprefix("gs://").split("/", 1)
        fake._staged_rows = storage.objects[(bucket, name)].decode().count("\n") - 1
        return original_load(source_uri, target_table, write_disposition)

    fake.load = load
    return fake


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(milliseconds=100)
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def processor(settings, storage, registry, audit, loader, clock, tmp_path) -> FileProcessor:
    return FileProcessor(settings, storage, registry, audit, loader, clock=clock, workdir=str(tmp_path))


def make_envelope(
    name: str, bucket: str = LANDING, generation: str = "1001", event_type: str = "OBJECT_FINALIZE"
) -> PushEnvelope:
    obj = {
        "bucket": bucket,
        "name": name,
        "generation": generation,
        "size": "123",
        "contentType": "text/csv",
        "timeCreated": "2026-10-15T08:59:00.000Z",
    }
    return PushEnvelope.model_validate(
        {
            "message": {
                "data": base64.b64encode(json.dumps(obj).encode()).decode(),
                "attributes": {
                    "eventType": event_type,
                    "bucketId": bucket,
                    "objectId": name,
                    "objectGeneration": generation,
                    "payloadFormat": "JSON_API_V1",
                },
                "messageId": "msg-1",
                "publishTime": "2026-10-15T08:59:01.000Z",
            },
            "subscription": "projects/proj/subscriptions/file-processor-push",
            "deliveryAttempt": 1,
        }
    )
