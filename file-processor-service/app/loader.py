"""Write the enriched CSV and run the BigQuery load job."""

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from google.cloud import bigquery

from app import coercion
from app.contracts import ContractError
from app.models import Contract
from app.validator import iter_csv

AUDIT_COLUMNS = ("_job_id", "_source_file", "_loaded_at")


@dataclass(frozen=True)
class LoadResult:
    job_id: str
    output_rows: int


def write_enriched_csv(
    src: Path,
    dst: Path,
    contract: Contract,
    table_columns: list[str],
    job_id: str,
    source_uri: str,
    loaded_at: datetime,
) -> int:
    """Rewrite a validated file with normalized values, in table column order, plus audit columns.

    Table columns the file doesn't have are written as NULL. A value that failed a WARNING-severity
    type check is also written as NULL so the load doesn't fail on it.
    """
    unknown = [c for c in contract.column_names if c not in table_columns]
    if unknown:
        raise ContractError(f"Contract columns not in table {contract.target_table}: {unknown}")

    specs = {c.name: (i, c) for i, c in enumerate(contract.columns)}
    audit_values = {"_job_id": job_id, "_source_file": source_uri, "_loaded_at": loaded_at.isoformat(sep=" ")}

    rows = 0
    reader = iter_csv(src, contract)
    if contract.has_header:
        next(reader, None)
    with dst.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(table_columns)
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            out = []
            for name in table_columns:
                if name in audit_values:
                    out.append(audit_values[name])
                elif name in specs:
                    idx, spec = specs[name]
                    out.append(_normalize_or_null(spec, row[idx]))
                else:
                    out.append("")
            writer.writerow(out)
            rows += 1
    return rows


def _normalize_or_null(spec, raw: str) -> str:
    if raw.strip() == "":
        return ""
    try:
        return coercion.normalize(spec, raw)
    except ValueError:
        return ""


class BigQueryLoader:
    def __init__(self, client: bigquery.Client, project: str, timeout_seconds: int = 480) -> None:
        self._client = client
        self._project = project
        self._timeout = timeout_seconds

    def table_id(self, target_table: str) -> str:
        return f"{self._project}.{target_table}"

    def table_columns(self, target_table: str) -> list[str]:
        return [f.name for f in self._client.get_table(self.table_id(target_table)).schema]

    def load(self, source_uri: str, target_table: str, write_disposition: str) -> LoadResult:
        config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.CSV,
            skip_leading_rows=1,
            write_disposition=write_disposition,
            allow_quoted_newlines=True,
            encoding="UTF-8",
            max_bad_records=0,
        )
        job = self._client.load_table_from_uri(
            source_uri, self.table_id(target_table), job_config=config, job_id_prefix="file_processor_"
        )
        job.result(timeout=self._timeout)
        return LoadResult(job_id=job.job_id, output_rows=job.output_rows or 0)
