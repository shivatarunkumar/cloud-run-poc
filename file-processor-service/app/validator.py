"""Validate a CSV file against its data contract.

All checks run in a single streaming pass. Every configured rule produces one
RuleResult; the file is loadable only if no ERROR-severity rule fails.
"""

import csv
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import coercion
from app.models import ColumnSpec, Contract, RuleResult, Stage


def csv_encoding(contract: Contract) -> str:
    # utf-8-sig transparently drops a BOM (common in files saved by Excel).
    return (
        "utf-8-sig" if contract.encoding.lower().replace("_", "-") in ("utf-8", "utf8") else contract.encoding
    )


def iter_csv(path: Path, contract: Contract) -> Iterator[list[str]]:
    with path.open(newline="", encoding=csv_encoding(contract)) as f:
        yield from csv.reader(f, delimiter=contract.delimiter)


@dataclass
class ValidationReport:
    results: list[RuleResult]
    rows_in_file: int
    # Rows failing any ERROR rule (contract or custom): drives job_metadata.rows_failed_validation.
    failed_rows: set[int] = field(default_factory=set)
    # Rows failing contract (schema/column) checks, split by severity, for the data_contract row.
    contract_failed_rows: set[int] = field(default_factory=set)
    contract_warned_rows: set[int] = field(default_factory=set)

    @property
    def passed(self) -> bool:
        return not any(r.is_blocking for r in self.results)

    @property
    def rules_failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)


class _RuleCounter:
    """Accumulates pass/fail counts and sample failures for one rule."""

    def __init__(self, result: RuleResult, max_samples: int) -> None:
        self.result = result
        self.evaluated = 0
        self.failed = 0
        self._max_samples = max_samples
        # values seen so far, for UNIQUE
        self.seen: set[str] = set()

    def check(self, ok: bool, row_number: int, value: Any) -> bool:
        self.evaluated += 1
        if not ok:
            self.failed += 1
            if len(self.result.sample_failures) < self._max_samples:
                self.result.sample_failures.append({"row_number": row_number, "value": value})
        return ok

    def finish(self) -> RuleResult:
        self.result.records_evaluated = self.evaluated
        self.result.records_failed = self.failed
        self.result.passed = self.failed == 0
        return self.result


def _column_rules(col: ColumnSpec, max_samples: int) -> dict[str, _RuleCounter]:
    def rule(rule_type: str, expected: str | None, params: dict[str, Any] | None = None) -> _RuleCounter:
        return _RuleCounter(
            RuleResult(
                stage=Stage.ROW_LEVEL,
                rule_name=f"{col.name}_{rule_type.lower()}",
                rule_type=rule_type,
                severity=col.severity,
                column_name=col.name,
                expected_value=expected,
                rule_params=params,
            ),
            max_samples,
        )

    rules: dict[str, _RuleCounter] = {}
    if col.required:
        rules["NOT_NULL"] = rule("NOT_NULL", "non-empty")
    if col.type != "STRING":
        rules["TYPE"] = rule("TYPE", col.type, {"format": col.format} if col.format else None)
    if col.regex:
        rules["REGEX"] = rule("REGEX", col.regex, {"regex": col.regex})
    if col.allowed_values is not None:
        rules["ALLOWED_VALUES"] = rule(
            "ALLOWED_VALUES", ",".join(col.allowed_values), {"allowed_values": col.allowed_values}
        )
    if col.min_length is not None:
        rules["MIN_LENGTH"] = rule("MIN_LENGTH", str(col.min_length), {"min_length": col.min_length})
    if col.max_length is not None:
        rules["MAX_LENGTH"] = rule("MAX_LENGTH", str(col.max_length), {"max_length": col.max_length})
    if col.min is not None or col.max is not None:
        rules["RANGE"] = rule("RANGE", _range_text(col), {"min": col.min, "max": col.max})
    if col.unique:
        rules["UNIQUE"] = rule("UNIQUE", "unique")
    return rules


def _range_text(col: ColumnSpec) -> str:
    if col.min is not None and col.max is not None:
        return f"{col.min:g}..{col.max:g}"
    return f">= {col.min:g}" if col.min is not None else f"<= {col.max:g}"


def _in_range(col: ColumnSpec, value: str) -> bool:
    try:
        n = float(value)
    except ValueError:
        return False
    return (col.min is None or n >= col.min) and (col.max is None or n <= col.max)


def _check_value(col: ColumnSpec, rules: dict[str, _RuleCounter], raw: str, row_number: int) -> bool:
    """Run all rules for one cell. Returns False if any rule failed (of the column's severity)."""
    ok = True
    is_null = raw.strip() == ""
    if "NOT_NULL" in rules:
        ok &= rules["NOT_NULL"].check(not is_null, row_number, raw)
    if is_null:
        return ok

    if "TYPE" in rules:
        try:
            coercion.normalize(col, raw)
            type_ok = True
        except ValueError:
            type_ok = False
        ok &= rules["TYPE"].check(type_ok, row_number, raw)
    if "REGEX" in rules:
        ok &= rules["REGEX"].check(bool(col.compiled_regex.fullmatch(raw.strip())), row_number, raw)
    if "ALLOWED_VALUES" in rules:
        ok &= rules["ALLOWED_VALUES"].check(raw.strip() in col.allowed_values, row_number, raw)
    if "MIN_LENGTH" in rules:
        ok &= rules["MIN_LENGTH"].check(len(raw) >= col.min_length, row_number, raw)
    if "MAX_LENGTH" in rules:
        ok &= rules["MAX_LENGTH"].check(len(raw) <= col.max_length, row_number, raw)
    if "RANGE" in rules:
        ok &= rules["RANGE"].check(_in_range(col, raw.strip()), row_number, raw)
    if "UNIQUE" in rules:
        counter, value = rules["UNIQUE"], raw.strip()
        ok &= counter.check(value not in counter.seen, row_number, raw)
        counter.seen.add(value)
    return ok


def validate(path: Path, contract: Contract, max_samples: int = 10) -> ValidationReport:
    file_rules: list[RuleResult] = []
    expected_header = contract.column_names
    n_cols = len(expected_header)

    column_rules = {c.name: _column_rules(c, max_samples) for c in contract.columns}
    column_count = _RuleCounter(
        RuleResult(
            stage=Stage.FILE_LEVEL,
            rule_name="column_count",
            rule_type="COLUMN_COUNT",
            expected_value=str(n_cols),
        ),
        max_samples,
    )
    unique = (
        _RuleCounter(
            RuleResult(
                stage=Stage.ROW_LEVEL,
                rule_name="unique_key",
                rule_type="UNIQUE",
                column_name=",".join(contract.unique_key),
                expected_value="unique",
                rule_params={"unique_key": contract.unique_key},
                category="custom",
            ),
            max_samples,
        )
        if contract.unique_key
        else None
    )
    key_idx = [expected_header.index(k) for k in contract.unique_key]
    seen_keys: set[tuple[str, ...]] = set()

    rows = 0
    failed_rows: set[int] = set()
    contract_failed: set[int] = set()
    contract_warned: set[int] = set()

    def report(results: list[RuleResult], rows_in_file: int) -> ValidationReport:
        return ValidationReport(results, rows_in_file, failed_rows, contract_failed, contract_warned)

    try:
        reader = iter_csv(path, contract)
        if contract.has_header:
            header = next(reader, None)
            actual = [h.strip() for h in header] if header else []
            header_ok = actual == expected_header
            file_rules.append(
                RuleResult(
                    stage=Stage.FILE_LEVEL,
                    rule_name="header_match",
                    rule_type="HEADER_MATCH",
                    expected_value=",".join(expected_header),
                    actual_value=",".join(actual),
                    passed=header_ok,
                    error_message=None if header_ok else _header_diff(expected_header, actual),
                )
            )
            if not header_ok:
                # Columns can't be trusted; row-level checks would only produce noise.
                return report(file_rules, _count_rows(path, contract))

        for row_number, row in enumerate(reader, start=1):
            if not any(cell.strip() for cell in row):
                continue  # ignore blank lines
            rows += 1
            if not column_count.check(len(row) == n_cols, row_number, len(row)):
                failed_rows.add(row_number)
                contract_failed.add(row_number)
                continue

            for col, raw in zip(contract.columns, row, strict=True):
                if not _check_value(col, column_rules[col.name], raw, row_number):
                    (contract_failed if col.severity == "ERROR" else contract_warned).add(row_number)
            if unique:
                key = tuple(row[i].strip() for i in key_idx)
                if not unique.check(
                    key not in seen_keys, row_number, dict(zip(contract.unique_key, key, strict=True))
                ):
                    failed_rows.add(row_number)
                seen_keys.add(key)
            if row_number in contract_failed:
                failed_rows.add(row_number)
    except UnicodeDecodeError as exc:
        file_rules.append(
            RuleResult(
                stage=Stage.FILE_LEVEL,
                rule_name="encoding",
                rule_type="ENCODING",
                expected_value=contract.encoding,
                passed=False,
                error_message=f"File is not valid {contract.encoding}: {exc}",
            )
        )
        return report(file_rules, rows)
    except csv.Error as exc:
        file_rules.append(
            RuleResult(
                stage=Stage.FILE_LEVEL,
                rule_name="csv_parse",
                rule_type="CSV_PARSE",
                passed=False,
                error_message=f"Malformed CSV near data row {rows + 1}: {exc}",
            )
        )
        return report(file_rules, rows)

    file_rules.insert(
        0,
        RuleResult(
            stage=Stage.FILE_LEVEL,
            rule_name="encoding",
            rule_type="ENCODING",
            expected_value=contract.encoding,
        ),
    )
    file_rules.append(_row_count_rule(contract, rows))
    file_rules.append(column_count.finish())

    results = file_rules + [r.finish() for rules in column_rules.values() for r in rules.values()]
    if unique:
        results.append(unique.finish())
    return report(results, rows)


def _row_count_rule(contract: Contract, rows: int) -> RuleResult:
    expected = f">= {contract.min_rows}" + (f" and <= {contract.max_rows}" if contract.max_rows else "")
    ok = rows >= contract.min_rows and (contract.max_rows is None or rows <= contract.max_rows)
    return RuleResult(
        stage=Stage.FILE_LEVEL,
        rule_name="row_count",
        rule_type="ROW_COUNT",
        expected_value=expected,
        actual_value=str(rows),
        passed=ok,
        error_message=None if ok else f"File has {rows} data rows, expected {expected}",
        rule_params={"min_rows": contract.min_rows, "max_rows": contract.max_rows},
    )


def _header_diff(expected: list[str], actual: list[str]) -> str:
    missing = [c for c in expected if c not in actual]
    extra = [c for c in actual if c not in expected]
    parts = []
    if missing:
        parts.append(f"missing columns: {missing}")
    if extra:
        parts.append(f"unexpected columns: {extra}")
    if not parts:
        parts.append("columns are in the wrong order")
    return "; ".join(parts)


def _count_rows(path: Path, contract: Contract) -> int:
    try:
        rows = sum(1 for row in iter_csv(path, contract) if any(c.strip() for c in row))
    except (UnicodeDecodeError, csv.Error):
        return 0
    return max(rows - (1 if contract.has_header else 0), 0)
