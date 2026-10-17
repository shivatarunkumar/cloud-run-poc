# Starter contracts

`customer.yaml`, `user.yaml` and `orders.yaml` describe the three `supplementary_data` tables.
They are the service's test fixtures (`file-processor-service/tests`), and the contracts the
pipeline started with.

**The live contracts are not managed here.** DataDesk publishes one YAML per onboarded
BigQuery table with File upload turned on, to `gs://<project>-<env>-config/contracts/<dataset>.<table>.yaml`.
It rewrites the file whenever the contract changes in DataDesk, and replaces any other
contract for the same table. Terraform no longer uploads these files.

To run the pipeline without DataDesk, copy these to the bucket: `make upload-contracts`.

The format is in [project.md §3](../../project.md). DataDesk adds `unique`, `min`, `max`,
`min_length` and `description` to columns.
