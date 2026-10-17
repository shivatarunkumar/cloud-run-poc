resource "google_pubsub_topic" "file_landing" {
  name   = "file-landing"
  labels = local.labels

  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "file_landing_dlq" {
  name   = "file-landing-dlq"
  labels = local.labels

  depends_on = [google_project_service.apis]
}

resource "google_pubsub_subscription" "file_landing_dlq" {
  name                       = "file-landing-dlq-sub"
  topic                      = google_pubsub_topic.file_landing_dlq.id
  message_retention_duration = "604800s"
  labels                     = local.labels
}

# GCS -> Pub/Sub notification for new objects in the landing bucket.
resource "google_storage_notification" "landing" {
  bucket         = google_storage_bucket.landing.name
  topic          = google_pubsub_topic.file_landing.id
  payload_format = "JSON_API_V1"
  event_types    = ["OBJECT_FINALIZE"]

  depends_on = [google_pubsub_topic_iam_member.gcs_publisher]
}

resource "google_pubsub_subscription" "file_processor_push" {
  name                 = "file-processor-push"
  topic                = google_pubsub_topic.file_landing.id
  ack_deadline_seconds = 600
  labels               = local.labels

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.file_processor.uri}/pubsub"

    oidc_token {
      service_account_email = google_service_account.pubsub_invoker.email
      audience              = google_cloud_run_v2_service.file_processor.uri
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.file_landing_dlq.id
    max_delivery_attempts = 5
  }

  depends_on = [
    google_pubsub_topic_iam_member.dlq_publisher,
    google_cloud_run_v2_service_iam_member.pubsub_invoker,
  ]
}

# --- contract changes ------------------------------------------------------------------------
# A contract YAML written or removed in the config bucket (DataDesk publishes them) → topic
# contract-changes → push to the service's /contracts route, which reads that contract again.
# The push reaches one instance; the others catch up when a file names a newer contract version
# (DataDesk tags every file) or their cached copy expires (CONTRACT_CACHE_TTL_SECONDS).
resource "google_pubsub_topic" "contract_changes" {
  name   = "contract-changes"
  labels = local.labels

  depends_on = [google_project_service.apis]
}

resource "google_storage_notification" "contracts" {
  bucket             = google_storage_bucket.config.name
  topic              = google_pubsub_topic.contract_changes.id
  payload_format     = "JSON_API_V1"
  event_types        = ["OBJECT_FINALIZE", "OBJECT_DELETE"]
  object_name_prefix = "contracts/"

  depends_on = [google_pubsub_topic_iam_member.gcs_contract_publisher]
}

resource "google_pubsub_subscription" "contract_refresh_push" {
  name                 = "contract-refresh-push"
  topic                = google_pubsub_topic.contract_changes.id
  ack_deadline_seconds = 60
  labels               = local.labels

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.file_processor.uri}/contracts"

    oidc_token {
      service_account_email = google_service_account.pubsub_invoker.email
      audience              = google_cloud_run_v2_service.file_processor.uri
    }
  }

  # no dead-letter topic: a refresh that never arrives is covered by the version check and TTL
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  depends_on = [google_cloud_run_v2_service_iam_member.pubsub_invoker]
}
