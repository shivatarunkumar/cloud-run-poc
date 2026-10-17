# --- Service accounts -------------------------------------------------------

resource "google_service_account" "file_processor" {
  account_id   = "file-processor-sa"
  display_name = "File processor Cloud Run runtime"

  depends_on = [google_project_service.apis]
}

resource "google_service_account" "pubsub_invoker" {
  account_id   = "pubsub-invoker-sa"
  display_name = "Pub/Sub push invoker for file processor"

  depends_on = [google_project_service.apis]
}

# --- Runtime SA: buckets ----------------------------------------------------

resource "google_storage_bucket_iam_member" "processor_object_admin" {
  for_each = {
    landing = google_storage_bucket.landing.name
    invalid = google_storage_bucket.invalid.name
    archive = google_storage_bucket.archive.name
    staging = google_storage_bucket.staging.name
  }

  bucket = each.value
  role   = "roles/storage.objectAdmin"
  member = google_service_account.file_processor.member
}

resource "google_storage_bucket_iam_member" "processor_config_viewer" {
  bucket = google_storage_bucket.config.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.file_processor.member
}

# --- Runtime SA: BigQuery ---------------------------------------------------

resource "google_bigquery_dataset_iam_member" "processor_data_editor" {
  for_each = {
    supplementary_data = google_bigquery_dataset.supplementary_data.dataset_id
    file_audit         = google_bigquery_dataset.file_audit.dataset_id
  }

  dataset_id = each.value
  role       = "roles/bigquery.dataEditor"
  member     = google_service_account.file_processor.member
}

resource "google_project_iam_member" "processor_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = google_service_account.file_processor.member
}

# --- Pub/Sub wiring ---------------------------------------------------------

# GCS needs to publish notifications to the topic.
resource "google_pubsub_topic_iam_member" "gcs_publisher" {
  topic  = google_pubsub_topic.file_landing.id
  role   = "roles/pubsub.publisher"
  member = local.gcs_service_agent

  depends_on = [google_project_service.apis]
}

# Pub/Sub must mint OIDC tokens for the invoker SA.
resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.pubsub_invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.pubsub_service_agent
}

# Dead-lettering requires publisher on the DLQ and subscriber on the source sub.
resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  topic  = google_pubsub_topic.file_landing_dlq.id
  role   = "roles/pubsub.publisher"
  member = local.pubsub_service_agent
}

resource "google_pubsub_subscription_iam_member" "dlq_subscriber" {
  subscription = google_pubsub_subscription.file_processor_push.id
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_service_agent
}

resource "google_cloud_run_v2_service_iam_member" "pubsub_invoker" {
  name     = google_cloud_run_v2_service.file_processor.name
  location = google_cloud_run_v2_service.file_processor.location
  role     = "roles/run.invoker"
  member   = google_service_account.pubsub_invoker.member
}
