"""FastAPI entry point. Pub/Sub pushes GCS notifications to POST /pubsub.

Endpoints are sync `def`: the GCP clients are blocking, so FastAPI runs them in its threadpool.
"""

import logging
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from google.cloud import bigquery, storage
from pydantic import ValidationError

from app.audit import AuditRepository
from app.config import get_settings
from app.contracts import ContractRegistry, gcs_contract, gcs_source, key_of
from app.loader import BigQueryLoader
from app.logging_config import configure_logging, log_context, trace_field
from app.models import GcsObjectEvent, PushEnvelope
from app.processor import FileProcessor, is_transient
from app.storage import GcsStorage

configure_logging()
log = logging.getLogger(__name__)

app = FastAPI(title="file-processor")


@lru_cache
def get_processor() -> FileProcessor:
    s = get_settings()
    gcs = storage.Client(project=s.gcp_project)
    bq = bigquery.Client(project=s.gcp_project, location=s.gcp_region)
    return FileProcessor(
        settings=s,
        storage=GcsStorage(gcs),
        contracts=ContractRegistry(
            gcs_source(gcs, s.config_bucket, s.contracts_prefix),
            s.contract_cache_ttl_seconds,
            fetch_one=gcs_contract(gcs, s.config_bucket, s.contracts_prefix),
        ),
        audit=AuditRepository(bq, s.gcp_project, s.audit_dataset, s.job_table, s.validation_table),
        loader=BigQueryLoader(bq, s.gcp_project, s.load_timeout_seconds),
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/pubsub")
def pubsub_push(
    request: Request,
    body: Annotated[dict[str, Any], Body()],
    processor: Annotated[FileProcessor, Depends(get_processor)],
) -> JSONResponse:
    message = body.get("message") if isinstance(body.get("message"), dict) else {}
    context = {
        **trace_field(request.headers.get("x-cloud-trace-context"), processor.s.gcp_project),
        "message_id": message.get("messageId") or message.get("message_id"),
    }
    with log_context(**context):
        log.debug("Raw Pub/Sub envelope", extra={"envelope": body})
        response = _handle(processor, body)
        log.info(
            "Response %d %s: %s",
            response.status_code,
            response.content["status"],
            response.content["detail"],
            extra={"http_status": response.status_code},
        )
        return JSONResponse(status_code=response.status_code, content=response.content)


@app.post("/contracts")
def contract_changed(
    body: Annotated[dict[str, Any], Body()],
    processor: Annotated[FileProcessor, Depends(get_processor)],
) -> Response:
    """A contract YAML changed in the config bucket (config bucket → Pub/Sub topic
    contract-changes → this push): read it again in this instance's cache. Other instances keep
    theirs until a file names a newer version, or the entry expires."""
    try:
        event = GcsObjectEvent.from_envelope(PushEnvelope.model_validate(body))
    except (ValidationError, ValueError) as exc:
        log.error("Malformed contract-change message: %s", exc, extra={"envelope": body})
        return JSONResponse(status_code=400, content={"status": "BAD_REQUEST", "detail": str(exc)[:500]})
    key = key_of(event.name, processor.s.contracts_prefix)
    if key is None or event.event_type not in ("OBJECT_FINALIZE", "OBJECT_DELETE"):
        log.info("Contract-change message ignored: %s %s", event.event_type, event.name)
        return Response(status_code=204)
    try:
        contract = processor.contracts.refresh(key)
    except Exception as exc:  # e.g. GCS unavailable: 500 makes Pub/Sub retry
        log.exception("Contract refresh failed for %s", key)
        return JSONResponse(status_code=500, content={"status": "ERROR", "detail": str(exc)[:500]})
    if contract is None:
        log.info("Contract removed: %s", key)
    else:
        log.info("Contract refreshed: %s v%s", key, contract.version)
    return Response(status_code=204)


class _Response:
    def __init__(self, status_code: int, status: str, detail: str, job_id: str | None = None) -> None:
        self.status_code = status_code
        self.content = {"status": status, "job_id": job_id, "detail": detail}


def _handle(processor: FileProcessor, body: dict[str, Any]) -> _Response:
    try:
        envelope = PushEnvelope.model_validate(body)
        outcome = processor.handle(envelope)
    except (ValidationError, ValueError) as exc:
        # Malformed message: 400 makes Pub/Sub retry and then dead-letter it, so it isn't lost silently.
        log.error("Malformed Pub/Sub message: %s", exc, extra={"envelope": body})
        return _Response(400, "BAD_REQUEST", str(exc)[:500])
    except Exception as exc:
        # Failure before a job could be recorded (e.g. BigQuery unavailable): retry.
        log.exception("Unhandled error before job was recorded (transient=%s)", is_transient(exc))
        return _Response(500, "ERROR", str(exc)[:500])
    return _Response(outcome.http_status, str(outcome.status), outcome.detail, outcome.job_id)
