resource "google_storage_bucket" "landing" {
  name                        = local.buckets.landing
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = local.labels

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket" "invalid" {
  name                        = local.buckets.invalid
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = local.labels

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket" "archive" {
  name                        = local.buckets.archive
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = local.labels

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket" "staging" {
  name                        = local.buckets.staging
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = local.labels

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket" "config" {
  name                        = local.buckets.config
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = local.labels

  depends_on = [google_project_service.apis]
}

# Data contracts are not managed here: DataDesk publishes one YAML per onboarded table to
# gs://<config bucket>/contracts/ (see config/contracts/README.md). Terraform only creates
# the bucket, so `make setup` never overwrites or deletes a contract DataDesk wrote.
