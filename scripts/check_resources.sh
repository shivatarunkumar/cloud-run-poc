#!/usr/bin/env bash
# Read-only check that every resource created by Infra/terraform exists.
# Usage: PROJECT_ID=... REGION=europe-west2 ENV=poc ./scripts/check_resources.sh
set -uo pipefail

: "${PROJECT_ID:?PROJECT_ID is required}"
REGION="${REGION:-europe-west2}"
ENV="${ENV:-poc}"
SERVICE_NAME="${SERVICE_NAME:-file-processor}"
PREFIX="${PROJECT_ID}-${ENV}"

pass=0
fail=0

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '  \033[32mOK\033[0m       %s\n' "$label"
    pass=$((pass + 1))
  else
    printf '  \033[31mMISSING\033[0m  %s\n' "$label"
    fail=$((fail + 1))
  fi
}

echo "Project: ${PROJECT_ID}  Region: ${REGION}  Env: ${ENV}"

echo "APIs"
enabled_apis="$(gcloud services list --enabled --project "$PROJECT_ID" --format='value(config.name)' 2>/dev/null)"
for api in run pubsub storage bigquery iam cloudbuild artifactregistry; do
  check "${api}.googleapis.com" grep -qx "${api}.googleapis.com" <<<"$enabled_apis"
done

echo "Buckets"
for b in landing invalid archive staging config; do
  check "gs://${PREFIX}-${b}" gcloud storage buckets describe "gs://${PREFIX}-${b}" --project "$PROJECT_ID"
done
check "contracts uploaded to gs://${PREFIX}-config/contracts/" \
  bash -c "gcloud storage ls 'gs://${PREFIX}-config/contracts/*.yaml' --project '$PROJECT_ID' | grep -q ."
# gcloud nests the topic under "notification_configuration", so match on the full output.
check "landing bucket notification -> file-landing" \
  bash -c "gcloud storage buckets notifications list 'gs://${PREFIX}-landing' --project '$PROJECT_ID' | grep -q 'topics/file-landing$'"

echo "Pub/Sub"
check "topic file-landing"              gcloud pubsub topics describe file-landing --project "$PROJECT_ID"
check "topic file-landing-dlq"          gcloud pubsub topics describe file-landing-dlq --project "$PROJECT_ID"
check "subscription file-processor-push" gcloud pubsub subscriptions describe file-processor-push --project "$PROJECT_ID"
check "subscription file-landing-dlq-sub" gcloud pubsub subscriptions describe file-landing-dlq-sub --project "$PROJECT_ID"

echo "BigQuery"
for t in supplementary_data.customer supplementary_data.user supplementary_data.orders \
         file_audit.job_metadata file_audit.validation_metadata; do
  check "${t}" bq --project_id="$PROJECT_ID" show --format=none "${PROJECT_ID}:${t}"
done

echo "Service accounts"
for sa in file-processor-sa pubsub-invoker-sa; do
  check "${sa}" gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" --project "$PROJECT_ID"
done

echo "Cloud Run"
check "service ${SERVICE_NAME}" gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT_ID"
image="$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT_ID" \
  --format='value(spec.template.spec.containers[0].image)' 2>/dev/null)"
if [[ -n "$image" ]]; then
  if [[ "$image" == *cloudrun/container/hello* ]]; then
    echo "  INFO     still on placeholder image — run 'make cloud-run-deploy'"
  else
    echo "  INFO     image: ${image}"
  fi
fi

echo
echo "Result: ${pass} OK, ${fail} missing"
[[ "$fail" -eq 0 ]]
