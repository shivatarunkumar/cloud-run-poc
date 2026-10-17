locals {
  prefix = "${var.project_id}-${var.env}"

  buckets = {
    landing = "${local.prefix}-landing"
    invalid = "${local.prefix}-invalid"
    archive = "${local.prefix}-archive"
    staging = "${local.prefix}-staging"
    config  = "${local.prefix}-config"
  }

  business_tables = ["customer", "user", "orders"]

  labels = {
    app = "file-processor"
    env = var.env
  }

  gcs_service_agent    = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
  pubsub_service_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
