"""Parse raw CSV strings into BigQuery-loadable values.

`normalize` returns the canonical text written to the enriched CSV, or raises
ValueError when the value doesn't fit the column type. Empty strings are NULLs
and are handled by the caller.
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from app.models import ColumnSpec

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
NUMERIC_MAX_SCALE = 9
NUMERIC_MAX_INTEGER_DIGITS = 29

_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}


def _int64(value: str) -> str:
    n = int(value)
    if not INT64_MIN <= n <= INT64_MAX:
        raise ValueError("out of INT64 range")
    return str(n)


def _numeric(value: str) -> str:
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("not a number") from exc
    if not d.is_finite():
        raise ValueError("not a finite number")
    sign, digits, exponent = d.as_tuple()
    scale = -exponent if exponent < 0 else 0
    if scale > NUMERIC_MAX_SCALE:
        raise ValueError(f"more than {NUMERIC_MAX_SCALE} decimal places")
    if len(digits) - scale > NUMERIC_MAX_INTEGER_DIGITS:
        raise ValueError("out of NUMERIC range")
    return format(d, "f")


def _float64(value: str) -> str:
    return repr(float(value))


def _bool(value: str) -> str:
    v = value.lower()
    if v in _TRUE:
        return "true"
    if v in _FALSE:
        return "false"
    raise ValueError("not a boolean")


# A contract `format` is tried first; ISO 8601 is always accepted too, since that is
# what the Excel converter writes for date cells.


def _date(value: str, fmt: str | None) -> str:
    try:
        d = datetime.strptime(value, fmt).date() if fmt else date.fromisoformat(value)
    except ValueError:
        if not fmt:
            raise
        d = date.fromisoformat(value)
    return d.isoformat()


def _timestamp(value: str, fmt: str | None) -> str:
    try:
        ts = datetime.strptime(value, fmt) if fmt else datetime.fromisoformat(value)
    except ValueError:
        if not fmt:
            raise
        ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat(sep=" ")


def normalize(column: ColumnSpec, raw: str) -> str:
    value = raw.strip()
    match column.type:
        case "STRING":
            return raw
        case "INT64":
            return _int64(value)
        case "NUMERIC":
            return _numeric(value)
        case "FLOAT64":
            return _float64(value)
        case "BOOL":
            return _bool(value)
        case "DATE":
            return _date(value, column.format)
        case "TIMESTAMP":
            return _timestamp(value, column.format)
    raise ValueError(f"unsupported type {column.type}")
