# cloud-run-poc — GCS → BigQuery file processor

A file dropped in the **landing** bucket triggers a Pub/Sub push to a Cloud Run service. The service validates the file against its data contract, loads it into the mapped BigQuery table, and records the run in the `file_audit` tables. See [project.md](project.md) for the full design.

```
landing bucket ──► Pub/Sub (file-landing) ──► Cloud Run (file-processor) ──► BigQuery (supplementary_data.*)
                         │                           ├──► file_audit.job_metadata / validation_metadata
                         └─► file-landing-dlq        └──► archive / invalid buckets
```

## Repository layout

| Path | What |
|---|---|
| `Infra/terraform/` | All GCP resources (Terraform) |
| `Infra/bq/` | BigQuery table schemas (JSON), used by Terraform |
| `config/contracts/` | Data contracts (YAML). Terraform uploads them to the config bucket |
| `file-processor-service/` | Cloud Run service (Python / FastAPI) |
| `mockdata/` | Sample files for end-to-end testing |
| `scripts/check_resources.sh` | Read-only check that all resources exist |
| `Makefile` | Entry point for everything below |

## Make targets

| Target | What it does |
|---|---|
| `make help` | Lists the targets |
| `make install` | Checks for CLI tools, creates `.venv` with the Python deps, runs `terraform init` |
| `make tfvars` | Creates `Infra/terraform/terraform.tfvars` from the example |
| `make plan` | `terraform plan`: shows what will be created. **Changes nothing.** |
| `make setup` | `terraform apply`: **creates the resources** (asks for `yes` first) |
| `make check-resources` | Verifies every expected resource exists (read-only) |
| `make cloud-run-deploy` | Builds the service from source and deploys it to Cloud Run |
| `make check` | Code checks: `terraform fmt`/`validate`, `ruff` lint and format |
| `make test` | Runs `pytest` for the service |
| `make bq-cleanup` | **Truncates every table** in `supplementary_data` and `file_audit` (keeps schemas). Lists the tables and asks you to type the project ID first. `DRY_RUN=1` only lists them |
| `make destroy` | `terraform destroy`: **deletes everything** (asks for `yes` first) |

`make` reads the project ID, region and env from `Infra/terraform/terraform.tfvars`. Override one-off with, for example, `make check-resources PROJECT_ID=other-project`.

---

## Step-by-step: create the resources

### 1. Prerequisites

- `gcloud` CLI (includes `bq`), Terraform ≥ 1.5, Python 3.12, `make`
- A GCP project with billing enabled
- Your account needs these roles on the project. **Owner** covers all of them; otherwise grant:

  | For | Roles |
  |---|---|
  | `make setup` | Service Usage Admin, Storage Admin, Pub/Sub Admin, BigQuery Admin, Cloud Run Admin, Service Account Admin, Service Account User, Project IAM Admin |
  | `make cloud-run-deploy` | Cloud Run Admin (or Developer), Service Account User (on `file-processor-sa`), Cloud Build Editor, Artifact Registry Admin (the first deploy creates the repo), Storage Admin (build source upload) |

### 2. Authenticate

```bash
gcloud auth login                          # for gcloud / bq commands
gcloud auth application-default login      # for Terraform
gcloud config set project <your-project-id>
```

### 3. Configure

```bash
make tfvars
```

Edit `Infra/terraform/terraform.tfvars`:

```hcl
project_id  = "<your-project-id>"
region      = "europe-west2"
bq_location = "europe-west2"
env         = "poc"
```

`terraform.tfvars` is git-ignored.

### 4. Install local tooling

```bash
make install
```

### 5. Review the plan

```bash
make plan
```

Expect about 40 resources to be created:

| Area | Resources |
|---|---|
| APIs | run, pubsub, storage, bigquery, iam, cloudbuild, artifactregistry |
| GCS | `<project>-<env>-landing`, `-invalid`, `-archive` (moves to Nearline after 30 days), `-staging` (deletes after 7 days), `-config` with contracts under `contracts/` |
| Pub/Sub | topic `file-landing` + bucket notification on landing; push subscription `file-processor-push` (OIDC, 600s ack, backoff 10–600s, DLQ after 5 attempts); topic `file-landing-dlq` + `file-landing-dlq-sub` |
| BigQuery (europe-west2) | `supplementary_data.customer`, `.user`, `.orders`; `file_audit.job_metadata`, `.validation_metadata` |
| IAM | `file-processor-sa` (runtime), `pubsub-invoker-sa` (push auth), plus bindings for the GCS and Pub/Sub service agents |
| Cloud Run | `file-processor` (internal ingress, 1 vCPU / 1 GiB, max 5 instances), initially on Google's `hello` placeholder image |

No VPC or subnet is needed. Cloud Run reaches GCS, BigQuery and Pub/Sub through Google APIs.

### 6. Create the resources

```bash
make setup        # review the plan output, then type: yes
```

The first apply can take a few minutes while the APIs enable. If it fails with an "API not enabled" or "service agent does not exist" error, wait a minute and run `make setup` again; Terraform picks up where it stopped.

### 7. Verify

```bash
make check-resources
```

Every line should print `OK`. The Cloud Run line also prints which image is deployed: the placeholder until step 8 is done.

### 8. Deploy the service code

```bash
make cloud-run-deploy
```

This runs `gcloud run deploy --source file-processor-service` with the runtime service account. Cloud Build builds the image and stores it in the `cloud-run-source-deploy` Artifact Registry repo, which gcloud creates on the first deploy. Env vars, scaling and ingress stay as Terraform set them. Terraform ignores the image, so running `make setup` again won't roll back a deploy.

> If the build fails with a permission error, the Cloud Build service account needs `roles/run.builder` on the project:
> `gcloud projects add-iam-policy-binding <project> --member=serviceAccount:<project-number>-compute@developer.gserviceaccount.com --role=roles/run.builder`

### 9. Test end to end with mock data

`mockdata/` has one file per scenario (regenerate them with `make mockdata`):

| File | Expected |
|---|---|
| `customer_20261015.csv`, `user_20261015.csv`, `orders_20261015.csv` | `SUCCESS`, 20 / 15 / 25 rows loaded, file in archive |
| `orders_20261016.xlsx` | `SUCCESS`, 10 rows (Excel converted to CSV) |
| `customer_20261017.csv` | `SUCCESS`, 5 rows, with a WARNING for the phone format in `validation_metadata` |
| `customer_20261016.csv` | `REJECTED` (bad email, null id, duplicate id, bad country/date/boolean), file in invalid |
| `user_20261016.csv` | `REJECTED`, `role` column missing (HEADER_MATCH) |
| `unknown_20261015.csv` | `REJECTED`, no contract, file in `invalid/_unmapped/` |

Upload one file to start with, then the rest:

```bash
make upload-mockdata FILES=mockdata/customer_20261015.csv
make upload-mockdata                       # all files
```

Check the results (replace `<project>`):

```bash
# One row per file
bq query --use_legacy_sql=false '
SELECT file_name, status, error_code, rows_in_file, rows_loaded, validation_rules_failed, final_file_path
FROM `<project>.file_audit.job_metadata`
WHERE DATE(created_at) = CURRENT_DATE()
ORDER BY created_at DESC'

# Why files failed or warned (one row per check; full per-column detail in `details`)
bq query --use_legacy_sql=false '
SELECT file_name, check_name, validation_result, summary, failed_checks, TO_JSON_STRING(details) AS details
FROM `<project>.file_audit.validation_metadata`
WHERE DATE(created_at) = CURRENT_DATE() AND validation_result != "PASS"
ORDER BY file_name, check_name'

# Loaded rows
bq query --use_legacy_sql=false '
SELECT "customer" t, COUNT(*) n FROM `<project>.supplementary_data.customer` UNION ALL
SELECT "user", COUNT(*) FROM `<project>.supplementary_data.user` UNION ALL
SELECT "orders", COUNT(*) FROM `<project>.supplementary_data.orders`'

# Where the files ended up (landing should be empty)
gcloud storage ls -r gs://<project>-poc-landing gs://<project>-poc-archive gs://<project>-poc-invalid

# Service logs: every line has job_id, file, message_id, contract
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="file-processor"
  AND jsonPayload.file:"customer_20261015.csv"' --limit 100 --format='value(timestamp, severity, jsonPayload.message)'

# All lines for one job
gcloud logging read 'jsonPayload.job_id="<job_id>"' --format='value(timestamp, severity, jsonPayload.message)'
```

Uploading the same file again processes it again. `customer` and `orders` are append-mode, so their rows are added again. `user` is overwrite-mode, so its table is replaced.

### 10. Updating

| Change | Command |
|---|---|
| Service code | `make cloud-run-deploy` |
| Contract YAML in `config/contracts/` | `make setup` (re-uploads changed contracts; the service picks them up within 5 minutes) |
| Infra / table schemas | `make plan`, then `make setup` |

### 11. Tear down

```bash
make destroy
```

This deletes all buckets (including their files), datasets and tables. The `cloud-run-source-deploy` registry repo was created by gcloud, so delete it by hand if you want it gone.

---

## Status

- [x] Infrastructure (Terraform, Makefile, this README)
- [x] Data contracts (`config/contracts/`: customer, user, orders)
- [x] Cloud Run service (`file-processor-service/`, see its [README](file-processor-service/README.md))
- [x] Mock data and end-to-end test steps
