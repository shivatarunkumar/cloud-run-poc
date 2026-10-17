# File processor PoC — run `make help` for targets.
#
# Project/region are read from Infra/terraform/terraform.tfvars so there is a
# single place to configure them. Override on the command line if needed:
#   make check-resources PROJECT_ID=my-project

SHELL := /bin/bash

TF_DIR      := Infra/terraform
TFVARS      := $(TF_DIR)/terraform.tfvars
SERVICE_DIR := file-processor-service
VENV        := .venv
PYTHON_BIN  ?= python3.12
PYTHON      := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip

tfvar = $(shell sed -n 's/^$(1)[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' $(TFVARS) 2>/dev/null)

PROJECT_ID   ?= $(call tfvar,project_id)
REGION       ?= $(or $(call tfvar,region),europe-west2)
ENV          ?= $(or $(call tfvar,env),poc)
SERVICE_NAME ?= file-processor
RUNTIME_SA   := file-processor-sa@$(PROJECT_ID).iam.gserviceaccount.com
LANDING      := gs://$(PROJECT_ID)-$(ENV)-landing
FILES        ?= mockdata/*.csv mockdata/*.xlsx
BQ_CLEANUP_DATASETS ?= supplementary_data file_audit

.DEFAULT_GOAL := help
.PHONY: help install tfvars setup plan destroy check-resources cloud-run-deploy check test require-project \
        mockdata upload-mockdata bq-cleanup

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

require-project:
	@if [ -z "$(PROJECT_ID)" ]; then \
		echo "PROJECT_ID is not set. Run 'make tfvars' and edit $(TFVARS), or pass PROJECT_ID=..."; exit 1; fi

# --- Local setup -------------------------------------------------------------

install: ## Check CLI tools and create the Python venv with dependencies
	@for t in gcloud terraform $(PYTHON_BIN); do \
		command -v $$t >/dev/null || { echo "Missing required tool: $$t"; exit 1; }; done
	@command -v bq >/dev/null || echo "Warning: 'bq' CLI not found (installed with gcloud); check-resources needs it"
	$(PYTHON_BIN) -m venv $(VENV)
	$(PIP) install --upgrade pip
	@if [ -f $(SERVICE_DIR)/requirements.txt ]; then $(PIP) install -r $(SERVICE_DIR)/requirements.txt; \
		else echo "Skipping $(SERVICE_DIR)/requirements.txt (not created yet)"; fi
	@if [ -f $(SERVICE_DIR)/requirements-dev.txt ]; then $(PIP) install -r $(SERVICE_DIR)/requirements-dev.txt; \
		else echo "Skipping $(SERVICE_DIR)/requirements-dev.txt (not created yet)"; fi
	terraform -chdir=$(TF_DIR) init -input=false

tfvars: ## Create terraform.tfvars from the example (won't overwrite)
	@if [ -f $(TFVARS) ]; then echo "$(TFVARS) already exists"; \
		else cp $(TF_DIR)/terraform.tfvars.example $(TFVARS) && echo "Created $(TFVARS) — set project_id"; fi

# --- Infrastructure ----------------------------------------------------------

plan: require-project ## Show what Terraform would create/change
	terraform -chdir=$(TF_DIR) init -input=false
	terraform -chdir=$(TF_DIR) plan

setup: require-project ## Create/update all GCP resources (terraform apply, asks for confirmation)
	terraform -chdir=$(TF_DIR) init -input=false
	terraform -chdir=$(TF_DIR) apply

destroy: require-project ## Delete all GCP resources created by Terraform (asks for confirmation)
	terraform -chdir=$(TF_DIR) destroy

bq-cleanup: require-project ## Truncate all tables in supplementary_data and file_audit (asks to confirm)
	@PROJECT_ID=$(PROJECT_ID) ./scripts/bq_cleanup.sh $(BQ_CLEANUP_DATASETS)

check-resources: require-project ## Verify every expected GCP resource exists
	@PROJECT_ID=$(PROJECT_ID) REGION=$(REGION) ENV=$(ENV) SERVICE_NAME=$(SERVICE_NAME) ./scripts/check_resources.sh

# --- Service -----------------------------------------------------------------

cloud-run-deploy: require-project ## Build from source and deploy the service to Cloud Run
	gcloud run deploy $(SERVICE_NAME) \
		--source $(SERVICE_DIR) \
		--project $(PROJECT_ID) \
		--region $(REGION) \
		--service-account $(RUNTIME_SA) \
		--no-allow-unauthenticated \
		--quiet

check: ## Code checks: ruff lint + format, terraform fmt + validate
	terraform -chdir=$(TF_DIR) fmt -check -recursive
	terraform -chdir=$(TF_DIR) init -input=false -backend=false >/dev/null
	terraform -chdir=$(TF_DIR) validate
	@if [ -d $(SERVICE_DIR)/app ]; then \
		$(VENV)/bin/ruff check $(SERVICE_DIR) && $(VENV)/bin/ruff format --check $(SERVICE_DIR); \
		else echo "Skipping Python checks ($(SERVICE_DIR)/app not created yet)"; fi

test: ## Run pytest for the service
	@if [ -d $(SERVICE_DIR)/tests ]; then cd $(SERVICE_DIR) && ../$(VENV)/bin/pytest -q; \
		else echo "Skipping tests ($(SERVICE_DIR)/tests not created yet)"; fi

# --- Mock data ---------------------------------------------------------------

mockdata: ## Regenerate the mock files in mockdata/
	$(VENV)/bin/python mockdata/generate.py

upload-mockdata: require-project ## Upload mock files to landing (all, or FILES="mockdata/x.csv ...")
	gcloud storage cp $(FILES) $(LANDING)/ --project $(PROJECT_ID)
