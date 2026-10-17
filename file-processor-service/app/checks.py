"""Roll fine-grained RuleResults up into validation_metadata rows (CheckRow).

One file produces a handful of rows instead of one per column rule:

    data_contract    every schema/column check; per-column results in `details`
    <custom rule>    one row per contract-level business rule (unique_key, ...)
    recon_row_count  post-load reconciliation
    contract_match / file_format   only when a file is rejected before validation

`details` stays small: passing checks are just {"result": "PASS", "evaluated": n};
only failing checks carry expected/actual values and sample failures.
"""

from typing import Any

from app.models import CheckRow, CheckType, Contract, RuleResult, Stage
from app.validator import ValidationReport


def _overall(rules: list[RuleResult]) -> str:
    if any(r.is_blocking for r in rules):
        return "FAIL"
    return "WARN" if any(not r.passed for r in rules) else "PASS"


def _compact(r: RuleResult) -> dict[str, Any]:
    if r.passed:
        out: dict[str, Any] = {"result": "PASS", "evaluated": r.records_evaluated}
    else:
        out = {
            "result": "FAIL",
            "severity": r.severity,
            "evaluated": r.records_evaluated,
            "failed": r.records_failed,
            "expected": r.expected_value,
            "actual": r.actual_value,
            "error": r.error_message,
            "samples": r.sample_failures or None,
        }
    return {k: v for k, v in out.items() if v is not None}


def _failure_text(r: RuleResult) -> str:
    if r.records_failed:
        return f"{r.check_label} ({r.records_failed} row{'s' if r.records_failed != 1 else ''})"
    return r.check_label


def contract_config(contract: Contract) -> dict[str, Any]:
    """Snapshot of the contract settings the data_contract check enforced."""
    return {
        "encoding": contract.encoding,
        "delimiter": contract.delimiter,
        "has_header": contract.has_header,
        "min_rows": contract.min_rows,
        "max_rows": contract.max_rows,
        "columns": [c.model_dump(exclude_defaults=True) for c in contract.columns],
    }


def data_contract_row(report: ValidationReport, contract: Contract) -> CheckRow:
    rules = [r for r in report.results if r.category == "contract"]
    failed = [r for r in rules if not r.passed]
    result = _overall(rules)

    file_level: dict[str, Any] = {}
    columns: dict[str, dict[str, Any]] = {}
    for r in rules:
        if r.stage == Stage.FILE_LEVEL:
            file_level[r.rule_type.lower()] = _compact(r)
        else:
            columns.setdefault(r.column_name, {})[r.rule_type.lower()] = _compact(r)

    if result == "PASS":
        summary = f"All {len(rules)} contract checks passed on {report.rows_in_file} rows"
    else:
        summary = f"{len(failed)} of {len(rules)} contract checks failed: " + ", ".join(
            _failure_text(r) for r in failed
        )

    return CheckRow(
        stage="PRE_LOAD",
        check_name="data_contract",
        check_type=CheckType.DATA_CONTRACT,
        result=result,
        summary=summary,
        severity="ERROR" if result == "FAIL" else ("WARNING" if result == "WARN" else None),
        records_evaluated=report.rows_in_file,
        records_failed=len(report.contract_failed_rows),
        records_warned=len(report.contract_warned_rows),
        checks_evaluated=len(rules),
        checks_failed=len(failed),
        failed_checks=[r.check_label for r in failed],
        details={"file": file_level, "columns": columns},
        check_config=contract_config(contract),
    )


def single_rule_row(
    r: RuleResult, check_type: CheckType, stage: str = "PRE_LOAD", summary: str | None = None
) -> CheckRow:
    failed_count = r.records_failed if not r.passed else 0
    if summary is None:
        if r.passed:
            summary = f"{r.rule_name} passed" + (
                f" on {r.records_evaluated} rows" if r.records_evaluated is not None else ""
            )
        else:
            summary = r.error_message or f"{r.rule_name} failed: {_failure_text(r)}"
    return CheckRow(
        stage=stage,
        check_name=r.rule_name,
        check_type=check_type,
        result=_overall([r]),
        summary=summary,
        severity=None if r.passed else r.severity,
        records_evaluated=r.records_evaluated,
        records_failed=failed_count if r.severity == "ERROR" else 0,
        records_warned=failed_count if r.severity == "WARNING" else 0,
        checks_failed=0 if r.passed else 1,
        failed_checks=[] if r.passed else [r.check_label],
        details=_compact(r),
        check_config=r.rule_params,
    )


def validation_rows(report: ValidationReport, contract: Contract) -> list[CheckRow]:
    rows = [data_contract_row(report, contract)]
    rows += [single_rule_row(r, CheckType.CUSTOM_RULE) for r in report.results if r.category == "custom"]
    return rows


def recon_row(rows_in_file: int | None, rows_loaded: int | None) -> CheckRow:
    ok = rows_in_file == rows_loaded
    rule = RuleResult(
        stage=Stage.POST_LOAD,
        rule_name="recon_row_count",
        rule_type="RECON_ROW_COUNT",
        expected_value=str(rows_in_file),
        actual_value=str(rows_loaded),
        records_evaluated=rows_in_file,
        records_failed=abs((rows_in_file or 0) - (rows_loaded or 0)),
        passed=ok,
        error_message=None if ok else "Rows loaded differ from rows in file",
    )
    summary = (
        f"{rows_loaded} rows loaded = {rows_in_file} rows in file"
        if ok
        else f"Loaded {rows_loaded} rows but file has {rows_in_file}"
    )
    return single_rule_row(rule, CheckType.RECONCILIATION, stage="POST_LOAD", summary=summary)


def gate_row(name: str, check_type: CheckType, expected: str, actual: str, message: str) -> CheckRow:
    """A pre-validation check that stopped the file (no contract, wrong format, ...)."""
    rule = RuleResult(
        stage=Stage.FILE_LEVEL,
        rule_name=name,
        rule_type=str(check_type),
        expected_value=expected,
        actual_value=actual,
        passed=False,
        error_message=message,
    )
    return single_rule_row(rule, check_type, summary=message)
