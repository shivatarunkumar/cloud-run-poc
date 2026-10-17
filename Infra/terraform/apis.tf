resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "storage.googleapis.com",
    "bigquery.googleapis.com",
    "iam.googleapis.com",
    # Used by `gcloud run deploy --source` from local.
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false
}
