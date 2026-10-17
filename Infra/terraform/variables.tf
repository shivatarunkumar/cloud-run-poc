variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "Region for all resources."
  type        = string
  default     = "europe-west2"
}

variable "bq_location" {
  description = "BigQuery dataset location."
  type        = string
  default     = "europe-west2"
}

variable "env" {
  description = "Environment name, used in resource names."
  type        = string
  default     = "poc"
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "file-processor"
}

variable "log_level" {
  description = "Service log level: DEBUG (also logs raw Pub/Sub envelopes), INFO, WARNING, ERROR."
  type        = string
  default     = "INFO"
}
