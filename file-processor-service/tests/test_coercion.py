import pytest

from app.coercion import normalize
from app.models import ColumnSpec


def col(type_: str, fmt: str | None = None) -> ColumnSpec:
    return ColumnSpec(name="c", type=type_, format=fmt)


@pytest.mark.parametrize(
    "type_, raw, expected",
    [
        ("STRING", " keep spaces ", " keep spaces "),
        ("INT64", " 42 ", "42"),
        ("INT64", "-7", "-7"),
        ("NUMERIC", "12.50", "12.50"),
        ("NUMERIC", "1e2", "100"),
        ("FLOAT64", "1.5", "1.5"),
        ("BOOL", "Yes", "true"),
        ("BOOL", "0", "false"),
        ("DATE", "2026-10-15", "2026-10-15"),
        ("TIMESTAMP", "2026-10-15T10:00:00Z", "2026-10-15 10:00:00+00:00"),
        ("TIMESTAMP", "2026-10-15 10:00:00", "2026-10-15 10:00:00+00:00"),
        ("TIMESTAMP", "2026-10-15T11:00:00+01:00", "2026-10-15 10:00:00+00:00"),
    ],
)
def test_normalize(type_, raw, expected):
    assert normalize(col(type_), raw) == expected


@pytest.mark.parametrize(
    "type_, raw",
    [
        ("INT64", "1.0"),
        ("INT64", str(2**63)),
        ("NUMERIC", "abc"),
        ("NUMERIC", "1.1234567891"),
        ("NUMERIC", "NaN"),
        ("FLOAT64", "x"),
        ("BOOL", "maybe"),
        ("DATE", "15/10/2026"),
        ("TIMESTAMP", "yesterday"),
    ],
)
def test_normalize_rejects(type_, raw):
    with pytest.raises(ValueError):
        normalize(col(type_), raw)


def test_custom_date_format_with_iso_fallback():
    c = col("DATE", "%d/%m/%Y")
    assert normalize(c, "15/10/2026") == "2026-10-15"
    assert normalize(c, "2026-10-15") == "2026-10-15"
    with pytest.raises(ValueError):
        normalize(c, "10-15-2026")
