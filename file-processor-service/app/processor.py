"""Orchestrates processing of one landed file (project.md §2).

HTTP status returned to Pub/Sub:
  200  terminal outcome (SUCCESS, REJECTED, DUPLICATE_SKIPPED, permanent FAILED, ignored) -> ack
  409  another attempt for the same object is still running -> retry later
  500  transient error -> retry with backoff, then DLQ
"""

import concurrent.futures
import logging
import tempfile
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import requests
from google.api_core import exceptions as gexc

from app import converter, validator
from app.audit import JobRecord, PriorAttempt
from app.checks import gate_row, recon_row, validation_rows
from app.config import Settings
from app.contracts import ContractError, ContractMatch, ContractRegistry
from app.loader import LoadResult, write_enriched_csv
from app.logging_config import bind, step
from app.models import CheckRow, CheckType, GcsObjectEvent, JobStatus, PushEnvelope

log = logging.getLogger(__name__)

UNMAPPED = "_unmapped"
TERMINAL_STATUSES = {JobStatus.SUCCESS, JobStatus.REJECTED}


# --- Dependencies (real implementations in storage.py / audit.py / loader.py) --


class Storage(Protocol):
    def download(self, bucket: str, name: str, dest: Path, generation: str | None = None) -> None: ...
    def upload(self, src: Path, bucket: str, name: str, content_type: str = "text/csv") -> str: ...
    def move(
        self, src_bucket: str, name: str, dst_bucket: str, dst_name: str, generation: str | None = None
    ) -> str: ...
    def delete(self, bucket: str, name: str) -> None: ...


class Audit(Protocol):
    def find_attempts(self, bucket: str, name: str, generation: str) -> list[PriorAttempt]: ...
    def insert_job(self, job: JobRecord) -> None: ...
    def update_job(self, job: JobRecord, updated_at: datetime) -> None: ...
    def insert_validations(self, job: JobRecord, checks: list[CheckRow], validated_at: datetime) -> None: ...


class Loader(Protocol):
    def table_id(self, target_table: str) -> str: ...
    def table_columns(self, target_table: str) -> list[str]: ...
    def load(self, source_uri: str, target_table: str, write_disposition: str) -> LoadResult: ...


# --- Outcome / errors ----------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    status: str
    http_status: int
    detail: str
    job_id: str | None = None


class Rejected(Exception):
    """File is not loadable (no contract, wrong format, failed validation). Ack and move to invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_TRANSIENT = (
    gexc.ServiceUnavailable,
    gexc.InternalServerError,
    gexc.BadGateway,
    gexc.GatewayTimeout,
    gexc.TooManyRequests,
    gexc.DeadlineExceeded,
    gexc.RetryError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    concurrent.futures.TimeoutError,
    ConnectionError,
    TimeoutError,
)
# BigQuery reports some retryable conditions (rate limits, concurrent DML conflicts) as 400/403.
_TRANSIENT_MARKERS = ("rateLimitExceeded", "Could not serialize access", "jobRateLimitExceeded")


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, _TRANSIENT):
        return True
    if isinstance(exc, gexc.GoogleAPICallError):
        return any(m in str(exc) for m in _TRANSIENT_MARKERS)
    return False


# --- Processor -------------------------------------------------------------------


class FileProcessor:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        contracts: ContractRegistry,
        audit: Audit,
        loader: Loader,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        workdir: str | None = None,
    ) -> None:
        self.s = settings
        self.storage = storage
        self.contracts = contracts
        self.audit = audit
        self.loader = loader
        self.now = clock
        self.workdir = workdir

    def handle(self, envelope: PushEnvelope) -> Outcome:
        """`envelope` must already be parsed; malformed envelopes are rejected by the HTTP layer."""
        event = GcsObjectEvent.from_envelope(envelope)
        bind(file=event.uri, generation=event.generation)
        log.info(
            "Message received: event=%s file=%s generation=%s size=%s delivery_attempt=%s",
            event.event_type,
            event.uri,
            event.generation,
            event.size,
            envelope.delivery_attempt,
            extra={
                "event_type": event.event_type,
                "object": event.model_dump(mode="json"),
                "publish_time": envelope.message.publish_time,
                "subscription": envelope.subscription,
                "delivery_attempt": envelope.delivery_attempt,
                "attributes": envelope.message.attributes,
            },
        )

        if event.event_type != "OBJECT_FINALIZE":
            return self._ignore(f"event type {event.event_type!r} is not processed")
        if event.name.endswith("/"):
            return self._ignore("folder placeholder")
        if event.bucket != self.s.landing_bucket:
            return self._ignore(
                f"bucket {event.bucket!r} is not the landing bucket {self.s.landing_bucket!r}"
            )

        prior = self.audit.find_attempts(event.bucket, event.name, event.generation)
        log.info(
            "Prior attempts for this object generation: %d %s",
            len(prior),
            [f"{a.status}@{a.created_at:%H:%M:%S}" for a in prior],
        )
        skip = self._check_prior_attempts(prior)
        if skip:
            log.info(
                "Skipping: %s (HTTP %d)", skip.detail, skip.http_status, extra={"status": str(skip.status)}
            )
            return skip

        resolve_error: ContractError | None = None
        try:
            match = self.contracts.resolve(event.name)
        except ContractError as exc:
            match, resolve_error = None, exc
        if match:
            c = match.contract
            bind(contract=c.name)
            log.info(
                "Contract resolved: %s v%s -> %s load_mode=%s file_date=%s",
                c.name,
                c.version,
                c.target_table,
                c.load_mode,
                match.file_date,
                extra={"contract_version": c.version, "file_pattern": c.file_pattern},
            )
        else:
            log.warning("No contract for %s: %s", event.basename, resolve_error or "no file_pattern matched")

        job = self._new_job(envelope, event, match, attempt=len(prior) + 1)
        bind(job_id=job.job_id)
        self.audit.insert_job(job)
        log.info("Job registered: job_id=%s attempt=%d", job.job_id, job.attempt_number)
        return self._process(job, event, match, resolve_error)

    def _ignore(self, reason: str) -> Outcome:
        log.info("Ignored: %s", reason)
        return Outcome("IGNORED", 200, reason)

    # -- idempotency ---------------------------------------------------------------

    def _check_prior_attempts(self, prior: list[PriorAttempt]) -> Outcome | None:
        for attempt in prior:
            if attempt.status in TERMINAL_STATUSES or (
                attempt.status == JobStatus.FAILED and attempt.final_file_path
            ):
                return Outcome(
                    JobStatus.DUPLICATE_SKIPPED, 200, f"already processed with status {attempt.status}"
                )
        running = [a for a in prior if a.status == JobStatus.IN_PROGRESS]
        for attempt in running:
            age = (self.now() - attempt.created_at).total_seconds()
            if age < self.s.in_progress_timeout_seconds:
                return Outcome(JobStatus.IN_PROGRESS, 409, f"another attempt started {age:.0f}s ago")
        return None

    def _new_job(
        self, envelope: PushEnvelope, event: GcsObjectEvent, match: ContractMatch | None, attempt: int
    ) -> JobRecord:
        now = self.now()
        contract = match.contract if match else None
        return JobRecord(
            job_id=str(uuid.uuid4()),
            job_name=contract.job_name if contract else "unmapped",
            created_at=now,
            pubsub_message_id=envelope.message.message_id,
            pubsub_subscription=envelope.subscription,
            delivery_attempt=envelope.delivery_attempt,
            attempt_number=attempt,
            is_retried=attempt > 1,
            status=JobStatus.IN_PROGRESS,
            start_time=now,
            source_bucket=event.bucket,
            file_name=event.name,
            file_path=event.uri,
            file_generation=event.generation,
            file_size_bytes=event.size,
            file_format=event.extension or None,
            file_date=match.file_date if match else None,
            file_created_at=event.time_created,
            contract_name=contract.name if contract else None,
            contract_version=contract.version if contract else None,
            target_table=self.loader.table_id(contract.target_table) if contract else None,
            load_mode=contract.load_mode if contract else None,
            gcp_project=self.s.gcp_project,
            gcp_region=self.s.gcp_region,
            service_name=self.s.k_service,
            service_revision=self.s.k_revision,
            service_version=self.s.service_version,
            created_by=self.s.runtime_service_account,
        )

    # -- main flow -------------------------------------------------------------------

    def _process(
        self,
        job: JobRecord,
        event: GcsObjectEvent,
        match: ContractMatch | None,
        resolve_error: ContractError | None = None,
    ) -> Outcome:
        staged: list[str] = []
        loaded = False
        try:
            with tempfile.TemporaryDirectory(dir=self.workdir) as tmp:
                if resolve_error is not None:
                    self._gate_failed(
                        job,
                        "contract_match",
                        CheckType.CONTRACT_MATCH,
                        "exactly one contract",
                        event.basename,
                        str(resolve_error),
                    )
                    raise Rejected("AMBIGUOUS_CONTRACT", str(resolve_error))
                if match is None:
                    message = f"No contract matches file name {event.basename!r}"
                    self._gate_failed(
                        job,
                        "contract_match",
                        CheckType.CONTRACT_MATCH,
                        "a contract file_pattern",
                        event.basename,
                        message,
                    )
                    raise Rejected("NO_CONTRACT", message)

                contract = match.contract
                if event.extension not in contract.file_format:
                    allowed = ",".join(contract.file_format)
                    message = f"Format {event.extension!r} not allowed by contract (allowed: {allowed})"
                    self._gate_failed(
                        job,
                        "file_format",
                        CheckType.FILE_FORMAT,
                        allowed,
                        event.extension,
                        message,
                    )
                    raise Rejected("UNSUPPORTED_FORMAT", message)

                local = Path(tmp) / event.basename
                with step(log, "download", uri=event.uri) as s:
                    self.storage.download(event.bucket, event.name, local, event.generation)
                    s["bytes"] = local.stat().st_size

                csv_path = local
                if event.extension == "xlsx":
                    csv_path = local.with_suffix(".csv")
                    with step(log, "xlsx_to_csv") as s:
                        s["lines"] = converter.xlsx_to_csv(local, csv_path)
                        name = f"converted/{job.job_id}/{csv_path.name}"
                        s["staged"] = self.storage.upload(csv_path, self.s.staging_bucket, name)
                        staged.append(name)

                with step(log, "validate") as s:
                    report = validator.validate(csv_path, contract, self.s.max_sample_failures)
                    s.update(
                        rows=report.rows_in_file,
                        rules=len(report.results),
                        rules_failed=report.rules_failed,
                        rows_failed=len(report.failed_rows),
                        passed=report.passed,
                    )
                checks = validation_rows(report, contract)
                for c in checks:
                    level = logging.INFO if c.result == "PASS" else logging.WARNING
                    log.log(
                        level,
                        "Check %s=%s: %s",
                        c.check_name,
                        c.result,
                        c.summary,
                        extra={"check": c.check_name, "failed_checks": c.failed_checks},
                    )
                self.audit.insert_validations(job, checks, self.now())
                job.set(
                    rows_in_file=report.rows_in_file,
                    rows_failed_validation=len(report.failed_rows),
                    validation_rules_executed=len(report.results),
                    validation_rules_failed=report.rules_failed,
                    validation_status="PASSED" if report.passed else "FAILED",
                )
                if not report.passed:
                    failed = [label for c in checks if c.result == "FAIL" for label in c.failed_checks]
                    raise Rejected("VALIDATION_FAILED", f"Failed checks: {', '.join(failed)}")

                enriched = Path(tmp) / f"{job.job_id}.csv"
                with step(log, "stage_load_file") as s:
                    s["rows"] = write_enriched_csv(
                        csv_path,
                        enriched,
                        contract,
                        self.loader.table_columns(contract.target_table),
                        job.job_id,
                        event.uri,
                        self.now(),
                    )
                    load_name = f"loads/{job.job_id}/{contract.name}.csv"
                    staged_uri = self.storage.upload(enriched, self.s.staging_bucket, load_name)
                    staged.append(load_name)
                    s["staged"] = staged_uri
                job.set(staged_file_path=staged_uri)

                with step(
                    log, "bq_load", table=job.target_table, write_disposition=contract.write_disposition
                ) as s:
                    result = self.loader.load(staged_uri, contract.target_table, contract.write_disposition)
                    loaded = True
                    s.update(bq_job_id=result.job_id, output_rows=result.output_rows)
                job.set(bq_load_job_id=result.job_id, rows_loaded=result.output_rows)

            return self._finish_loaded(job, event)

        except Rejected as exc:
            return self._finish_rejected(job, event, exc)
        except Exception as exc:
            if loaded:
                # Rows are in BigQuery; never let a retry load them twice.
                log.exception("Post-load step failed; job still marked loaded")
                return self._finish_loaded(job, event, post_load_error=exc)
            return self._finish_failed(job, event, exc)
        finally:
            for name in staged:
                self._best_effort(self.storage.delete, self.s.staging_bucket, name)
            if staged:
                log.debug("Staging files removed: %s", staged)

    def _finish_loaded(
        self, job: JobRecord, event: GcsObjectEvent, post_load_error: Exception | None = None
    ) -> Outcome:
        """Rows are in BigQuery: every step here is best-effort and the message is always acked."""
        recon = recon_row(job.rows_in_file, job.rows_loaded)
        recon_ok = recon.result == "PASS"
        level = logging.INFO if recon_ok else logging.ERROR
        log.log(level, "Check %s=%s: %s", recon.check_name, recon.result, recon.summary)
        self._best_effort(self.audit.insert_validations, job, [recon], self.now())
        job.set(recon_status="PASSED" if recon_ok else "FAILED")

        final = None
        if post_load_error is None:
            final = self._best_effort(self._move, event, self.s.archive_bucket, job)

        if not recon_ok:
            status, reason, code = JobStatus.FAILED, "Loaded row count does not match file", "RECON_MISMATCH"
        elif post_load_error is not None:
            status, reason, code = (
                JobStatus.SUCCESS,
                f"Loaded; post-load step failed: {post_load_error}",
                None,
            )
        elif final is None:
            status, reason, code = (
                JobStatus.SUCCESS,
                "Loaded; archive move failed, file left in landing",
                None,
            )
        else:
            status, reason, code = JobStatus.SUCCESS, "Loaded", None
        job.set(status=status, status_reason=reason, error_code=code, final_file_path=final)
        self._best_effort(self._close, job)
        return Outcome(status, 200, reason, job.job_id)

    def _finish_rejected(self, job: JobRecord, event: GcsObjectEvent, exc: Rejected) -> Outcome:
        log.warning("File rejected (%s): %s", exc.code, exc)
        try:
            final = self._move(event, self.s.invalid_bucket, job)
        except Exception as move_error:
            return self._finish_failed(job, event, move_error)
        job.set(
            status=JobStatus.REJECTED,
            status_reason=str(exc),
            error_code=exc.code,
            error_message=str(exc),
            final_file_path=final,
        )
        self._close(job)
        return Outcome(JobStatus.REJECTED, 200, str(exc), job.job_id)

    def _finish_failed(self, job: JobRecord, event: GcsObjectEvent, exc: Exception) -> Outcome:
        transient = is_transient(exc)
        kind = "transient, Pub/Sub will retry" if transient else "permanent, file moved to invalid"
        log.error("Job failed (%s): %s: %s", kind, type(exc).__name__, exc, exc_info=exc)
        final = None
        if not transient:
            final = self._best_effort(self._move, event, self.s.invalid_bucket, job)
        job.set(
            status=JobStatus.FAILED,
            status_reason="Transient error; Pub/Sub will retry" if transient else "Permanent error",
            error_code="TRANSIENT" if transient else type(exc).__name__,
            error_message=str(exc)[:1000],
            error_details={
                "exception": type(exc).__name__,
                "traceback": traceback.format_exception(exc)[-5:],
            },
            final_file_path=final,
        )
        self._best_effort(self._close, job)
        return Outcome(JobStatus.FAILED, 500 if transient else 200, str(exc)[:500], job.job_id)

    # -- helpers -----------------------------------------------------------------------

    def _gate_failed(
        self, job: JobRecord, name: str, check_type: CheckType, expected: str, actual: str, message: str
    ) -> None:
        check = gate_row(name, check_type, expected, actual, message)
        log.warning("Check %s=%s: %s", check.check_name, check.result, check.summary)
        self.audit.insert_validations(job, [check], self.now())
        job.set(validation_rules_executed=1, validation_rules_failed=1, validation_status="FAILED")

    def _move(self, event: GcsObjectEvent, dst_bucket: str, job: JobRecord) -> str:
        with step(log, "move_file", source=event.uri) as s:
            s["destination"] = self.storage.move(
                event.bucket, event.name, dst_bucket, self._dated_path(job, event), event.generation
            )
        return s["destination"]

    def _dated_path(self, job: JobRecord, event: GcsObjectEvent) -> str:
        """<contract>/<yyyy>/<mm>/<dd>/<job_id>/<file>: same-named files never overwrite each other."""
        folder = job.contract_name or UNMAPPED
        return f"{folder}/{job.created_at:%Y/%m/%d}/{job.job_id}/{event.basename}"

    def _close(self, job: JobRecord) -> None:
        end = self.now()
        job.set(end_time=end, duration_seconds=round((end - job.start_time).total_seconds(), 3))
        self.audit.update_job(job, end)
        log.info(
            "Job finished: status=%s rows_in_file=%s rows_loaded=%s duration=%ss reason=%s",
            job.status,
            job.rows_in_file,
            job.rows_loaded,
            job.duration_seconds,
            job.status_reason,
            extra={"status": str(job.status), "final_file_path": job.final_file_path},
        )

    @staticmethod
    def _best_effort(fn, *args):
        try:
            return fn(*args)
        except Exception:
            log.exception("Best-effort step %s failed", getattr(fn, "__name__", fn))
            return None
