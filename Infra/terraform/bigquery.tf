resource "google_bigquery_dataset" "supplementary_data" {
  dataset_id                 = "supplementary_data"
  location                   = var.bq_location
  description                = "Business tables loaded from landed files."
  labels                     = local.labels
  delete_contents_on_destroy = true

  depends_on = [google_project_service.apis]
}

resource "google_bigquery_dataset" "file_audit" {
  dataset_id                 = "file_audit"
  location                   = var.bq_location
  description                = "Audit tables for the file processor."
  labels                     = local.labels
  delete_contents_on_destroy = true

  depends_on = [google_project_service.apis]
}

resource "google_bigquery_table" "business" {
  for_each = toset(local.business_tables)

  dataset_id          = google_bigquery_dataset.supplementary_data.dataset_id
  table_id            = each.value
  schema              = file("${path.module}/../bq/${each.value}.json")
  deletion_protection = false
  labels              = local.labels

  time_partitioning {
    type  = "DAY"
    field = "_loaded_at"
  }
}

resource "google_bigquery_table" "job_metadata" {
  dataset_id          = google_bigquery_dataset.file_audit.dataset_id
  table_id            = "job_metadata"
  schema              = file("${path.module}/../bq/job_metadata.json")
  clustering          = ["status", "target_table"]
  deletion_protection = false
  labels              = local.labels

  time_partitioning {
    type  = "DAY"
    field = "created_at"
  }
}

resource "google_bigquery_table" "validation_metadata" {
  dataset_id          = google_bigquery_dataset.file_audit.dataset_id
  table_id            = "validation_metadata"
  schema              = file("${path.module}/../bq/validation_metadata.json")
  clustering          = ["job_id", "validation_result"]
  deletion_protection = false
  labels              = local.labels

  time_partitioning {
    type  = "DAY"
    field = "created_at"
  }
}
