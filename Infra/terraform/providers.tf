terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Local state for the PoC. Switch to a GCS backend when sharing:
  # backend "gcs" {
  #   bucket = "<project_id>-tfstate"
  #   prefix = "file-processor"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {}

# Looking this up also makes GCS create its service agent if the project
# doesn't have one yet (needed before granting it publish on the topic).
data "google_storage_project_service_account" "gcs" {
  depends_on = [google_project_service.apis]
}
