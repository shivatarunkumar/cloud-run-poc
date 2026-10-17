# Terraform owns the service configuration; the code is deployed from local
# with `gcloud run deploy --source`. The placeholder image is only used until
# the first deploy, and image changes are ignored so re-applies don't revert it.
resource "google_cloud_run_v2_service" "file_processor" {
  name                = var.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = false
  labels              = local.labels

  template {
    service_account                  = google_service_account.file_processor.email
    timeout                          = "600s"
    max_instance_request_concurrency = 4

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "LANDING_BUCKET"
        value = google_storage_bucket.landing.name
      }
      env {
        name  = "INVALID_BUCKET"
        value = google_storage_bucket.invalid.name
      }
      env {
        name  = "ARCHIVE_BUCKET"
        value = google_storage_bucket.archive.name
      }
      env {
        name  = "STAGING_BUCKET"
        value = google_storage_bucket.staging.name
      }
      env {
        name  = "CONFIG_BUCKET"
        value = google_storage_bucket.config.name
      }
      env {
        name  = "CONTRACTS_PREFIX"
        value = "contracts/"
      }
      env {
        name  = "AUDIT_DATASET"
        value = google_bigquery_dataset.file_audit.dataset_id
      }
      env {
        name  = "CONTRACT_CACHE_TTL_SECONDS"
        value = "300"
      }
      env {
        name  = "RUNTIME_SERVICE_ACCOUNT"
        value = google_service_account.file_processor.email
      }
      env {
        name  = "LOG_LEVEL"
        value = var.log_level
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [google_project_service.apis]
}
