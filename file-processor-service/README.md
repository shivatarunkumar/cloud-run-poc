# file-processor (Cloud Run service)

A FastAPI service that receives GCS `OBJECT_FINALIZE` notifications through a Pub/Sub push subscription. It validates the file against its data contract, loads it into BigQuery, and records the run in `file_audit`. See [../project.md](../project.md) §2 for the full flow.

| Endpoint | Purpose |
|---|---|
| `POST /pubsub` | Pub/Sub push target |
| `GET /healthz` | Liveness check |

## Response codes (what Pub/Sub does with them)

| Code | When | Pub/Sub |
|---|---|---|
| 200 | `SUCCESS`, `REJECTED`, `DUPLICATE_SKIPPED`, permanent `FAILED`, ignored events | ack |
| 400 | Malformed message | retry, then DLQ |
| 409 | Another attempt for the same file is still running | retry later |
| 500 | Transient error (GCP 5xx, timeouts, rate limits) | retry with backoff, then DLQ |

## Logging

The service writes structured JSON to stdout, which Cloud Logging turns into `jsonPayload` entries with the right severity.

- **Correlation fields on every line:** `message_id`, `file`, `generation`, `contract` and `job_id`, each added as soon as it's known. Cloud Run's trace ID is attached too, so in Logs Explorer all lines for one Pub/Sub message are grouped under its HTTP request.
- **One readable line per step.** The key values are in the message text and also stored as separate fields:

```
INFO  Message received: event=OBJECT_FINALIZE file=gs://…/customer_20261016.csv generation=… size=779 delivery_attempt=1
INFO  Prior attempts for this object generation: 0 []
INFO  Contract resolved: customer v1 -> supplementary_data.customer load_mode=append file_date=2026-10-16
INFO  Audit job_metadata INSERT job_id=… status=IN_PROGRESS
INFO  Job registered: job_id=… attempt=1
INFO  download done in 85 ms uri=gs://… bytes=779
INFO  validate done in 2 ms rows=10 rules=20 rules_failed=6 rows_failed=6 passed=False
WARN  Check data_contract=FAIL: 5 of 19 contract checks failed: customer_id.NOT_NULL (1 row), email.REGEX (1 row), …
WARN  Check unique_key=FAIL: unique_key failed: customer_id.UNIQUE (1 row)
INFO  Audit validation_metadata +2 rows: data_contract=FAIL, unique_key=FAIL
WARN  File rejected (VALIDATION_FAILED): Failed checks: …
INFO  move_file done in 120 ms source=gs://…landing/… destination=gs://…invalid/customer/2026/10/16/<job_id>/…
INFO  Audit job_metadata UPDATE job_id=… status=REJECTED error_code=VALIDATION_FAILED rows_in_file=10 …
INFO  Job finished: status=REJECTED rows_in_file=10 rows_loaded=None duration=1.9s reason=…
INFO  Response 200 REJECTED: Failed checks: …
```

- **Audit trail:** every `job_metadata` INSERT/UPDATE and every `validation_metadata` insert is logged. The exact values written are in the `audit_values` field.
- **Errors** are logged with the full traceback and marked transient (will retry) or permanent.
- **`LOG_LEVEL`** (Terraform variable `log_level`, default `INFO`). Set `DEBUG` to also log the raw Pub/Sub envelope and when each step starts.

Find logs:

```bash
gcloud logging read 'jsonPayload.job_id="<job_id>"' --format='value(timestamp, severity, jsonPayload.message)'
gcloud logging read 'jsonPayload.file:"customer_20261016.csv"' --format='value(timestamp, severity, jsonPayload.message)'
```

## Code map

| Module | Responsibility |
|---|---|
| `main.py` | HTTP layer and wiring of the real GCS/BQ clients |
| `processor.py` | Orchestration, idempotency, file moves, status/HTTP mapping |
| `contracts.py` | Load YAML contracts from the config bucket (TTL cache) and match file names |
| `validator.py` | File-level and row-level rules in a single streaming pass |
| `coercion.py` | Type parsing and normalization to BigQuery CSV format |
| `converter.py` | `.xlsx` → CSV |
| `loader.py` | Enriched CSV (table column order plus `_job_id`, `_source_file`, `_loaded_at`) and the BQ load job |
| `checks.py` | Rolls rule results up into `validation_metadata` rows (`data_contract`, custom rules, recon) |
| `audit.py` | `job_metadata` (DML insert/update), `validation_metadata` (streaming insert); logs every write |
| `logging_config.py` | JSON logs, per-request context (`job_id`, `file`, trace), step timing |
| `storage.py` | GCS download, upload, move and delete |

## Development

From the repo root:

```bash
make install   # .venv with runtime + dev deps (Python 3.12)
make test      # pytest
make check     # ruff lint + format check, terraform fmt + validate
```

The tests don't touch GCP: `tests/conftest.py` replaces GCS, BigQuery and the loader with in-memory fakes. They use the real contracts from `config/contracts/`.

## Running locally against your GCP project

This needs the resources from `make setup` and your own credentials (`gcloud auth application-default login`). Your account needs read/write on the buckets and datasets.

```bash
cd file-processor-service
export GCP_PROJECT=<project> GCP_REGION=europe-west2
export LANDING_BUCKET=$GCP_PROJECT-poc-landing INVALID_BUCKET=$GCP_PROJECT-poc-invalid \
       ARCHIVE_BUCKET=$GCP_PROJECT-poc-archive STAGING_BUCKET=$GCP_PROJECT-poc-staging \
       CONFIG_BUCKET=$GCP_PROJECT-poc-config
../.venv/bin/uvicorn app.main:app --port 8080 --reload
```

Simulate a notification for a file that's already in the landing bucket.

> If the landing bucket notification is active, the deployed service will also process the file. To test only locally, upload under a name that no contract matches, or do this before the first `make cloud-run-deploy` (the placeholder image acks and ignores messages).

```bash
FILE=customer_20261015.csv
GEN=$(gcloud storage objects describe gs://$LANDING_BUCKET/$FILE --format='value(generation)')
DATA=$(printf '{"bucket":"%s","name":"%s","generation":"%s"}' $LANDING_BUCKET $FILE $GEN | base64)
curl -s -X POST localhost:8080/pubsub -H 'Content-Type: application/json' -d "{
  \"message\": {\"messageId\": \"local-1\", \"data\": \"$DATA\",
               \"attributes\": {\"eventType\": \"OBJECT_FINALIZE\"}},
  \"subscription\": \"local\"}"
```

## Deploying

From the repo root, run `make cloud-run-deploy`. It deploys with `gcloud run deploy --source`. Env vars, scaling, ingress and the service account come from Terraform (`Infra/terraform/cloud_run.tf`), and a deploy doesn't change them.
