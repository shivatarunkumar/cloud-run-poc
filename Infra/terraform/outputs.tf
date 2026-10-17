output "buckets" {
  value = local.buckets
}

output "cloud_run_service" {
  value = google_cloud_run_v2_service.file_processor.name
}

output "cloud_run_uri" {
  value = google_cloud_run_v2_service.file_processor.uri
}

output "runtime_service_account" {
  value = google_service_account.file_processor.email
}

output "deploy_command" {
  description = "Run from file-processor-service/ to deploy the code."
  value       = "gcloud run deploy ${var.service_name} --source . --region ${var.region} --project ${var.project_id} --service-account ${google_service_account.file_processor.email}"
}
