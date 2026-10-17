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
