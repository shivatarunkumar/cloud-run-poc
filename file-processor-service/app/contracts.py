"""Find the data contract (YAML) for a landed file.

A file under a table folder, e.g. landing/datadesk/supplementary_data.customer/<id>/customer_20261015.csv,
names its table: only contracts/supplementary_data.customer.yaml is read, on demand, and cached per table.
A contract change refreshes the cache through POST /contracts (config bucket → Pub/Sub).
That is how DataDesk lands files, so the number of contracts in the bucket doesn't matter.

A file with no table folder (dropped into landing by hand) falls back to matching its name against
every contract's file_pattern, which reads them all.
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from google.cloud import storage
from pydantic import ValidationError

from app.models import Contract

log = logging.getLogger(__name__)

# Returns {source_name: yaml_text}.
ContractSource = Callable[[], dict[str, str]]
# Returns the YAML text of one contract by its key ("dataset.table"), or None if there is none.
ContractFetcher = Callable[[str], str | None]

# a folder named after a table: dataset.table
TABLE_FOLDER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


def table_of(object_name: str) -> str | None:
    """The table a landed file names through its folder (dataset.table), if any."""
    return next((seg for seg in object_name.split("/")[:-1] if TABLE_FOLDER.fullmatch(seg)), None)


class ContractError(Exception):
    """Contract configuration problem (permanent; retrying won't help)."""


@dataclass(frozen=True)
class ContractMatch:
    contract: Contract
    # None when the contract was found by table folder and the name doesn't follow its file_pattern
    match: re.Match[str] | None

    @property
    def file_date(self) -> date | None:
        return self.contract.file_date(self.match) if self.match else None


def gcs_source(client: storage.Client, bucket: str, prefix: str) -> ContractSource:
    def load() -> dict[str, str]:
        return {
            blob.name: blob.download_as_text()
            for blob in client.list_blobs(bucket, prefix=prefix)
            if blob.name.endswith((".yaml", ".yml"))
        }

    return load


def gcs_contract(client: storage.Client, bucket: str, prefix: str) -> ContractFetcher:
    def fetch(key: str) -> str | None:
        blob = client.bucket(bucket).get_blob(f"{prefix}{key}.yaml")
        return blob.download_as_text() if blob is not None else None

    return fetch


def directory_contract(path: str | Path) -> ContractFetcher:
    def fetch(key: str) -> str | None:
        file = Path(path) / f"{key}.yaml"
        return file.read_text() if file.exists() else None

    return fetch


def directory_source(path: str | Path) -> ContractSource:
    def load() -> dict[str, str]:
        return {p.name: p.read_text() for p in sorted(Path(path).glob("*.y*ml"))}

    return load


def parse_contracts(sources: dict[str, str]) -> list[Contract]:
    """Parse contracts; invalid ones are logged and skipped so one bad file doesn't stop all loads."""
    contracts: list[Contract] = []
    for source_name, text in sources.items():
        try:
            contracts.append(Contract.model_validate(yaml.safe_load(text)))
        except (yaml.YAMLError, ValidationError, TypeError) as exc:
            log.error("Skipping invalid contract %s: %s", source_name, exc)
    return contracts


def key_of(object_name: str, prefix: str = "contracts/") -> str | None:
    """The table key of a contract object: "contracts/ds.tbl.yaml" → "ds.tbl"; None otherwise."""
    if not object_name.startswith(prefix):
        return None
    stem = object_name[len(prefix) :].removesuffix(".yaml").removesuffix(".yml")
    return stem if TABLE_FOLDER.fullmatch(stem) and object_name.endswith((".yaml", ".yml")) else None


class ContractRegistry:
    """Contracts read from the bucket on demand and cached per table.

    The cache stays right three ways: POST /contracts refreshes a table when its contract changes
    (config bucket → Pub/Sub → this instance), a file tagged with another contract version reads
    it again at once (DataDesk tags every file), and every entry expires after `ttl_seconds`."""

    def __init__(
        self,
        source: ContractSource,
        ttl_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
        fetch_one: ContractFetcher | None = None,
    ) -> None:
        self._source = source
        self._fetch_one = fetch_one
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # table key -> (read at, contract or None)
        self._by_table: dict[str, tuple[float, Contract | None]] = {}
        # every contract, for files matched by name (no table folder)
        self._all: list[Contract] = []
        self._all_at: float | None = None

    def contracts(self) -> list[Contract]:
        """Every contract: only for a file without a table folder."""
        with self._lock:
            now = self._clock()
            if self._all_at is None or now - self._all_at >= self._ttl:
                self._all = parse_contracts(self._source())
                self._all_at = now
                log.info(
                    "Read %d contracts to match files by name: %s",
                    len(self._all),
                    ", ".join(f"{c.name} v{c.version}" for c in self._all),
                )
            return self._all

    def _read(self, key: str) -> Contract | None:
        text = self._fetch_one(key) if self._fetch_one else None
        parsed = parse_contracts({f"{key}.yaml": text}) if text is not None else []
        contract = parsed[0] if parsed else None
        if contract is not None and contract.target_table != key:
            raise ContractError(f"Contract {key}.yaml is for {contract.target_table}, not {key}")
        return contract

    def for_table(self, key: str, version: str | None = None) -> Contract | None:
        """The contract for one table (contracts/<dataset>.<table>.yaml), from the cache when it
        is fresh and, if the file names a version, that version; otherwise read now."""
        if self._fetch_one is None:
            return None
        with self._lock:
            now = self._clock()
            cached = self._by_table.get(key)
            fresh = cached is not None and now - cached[0] < self._ttl
            right_version = cached is not None and (
                version is None or (cached[1] is not None and cached[1].version == str(version))
            )
            if fresh and right_version:
                return cached[1]
            contract = self._read(key)
            self._by_table[key] = (now, contract)
        if contract is None:
            log.warning("No contract for %s (%s.yaml)", key, key)
        else:
            log.info("Read contract %s v%s for %s", contract.name, contract.version, key)
            if version is not None and str(version) != contract.version:
                log.warning(
                    "%s: file checked against v%s, the bucket has v%s", key, version, contract.version
                )
        return contract

    def refresh(self, key: str) -> Contract | None:
        """Read one table's contract now (its YAML changed or was removed), and drop the
        name-matching list so it is read again too."""
        with self._lock:
            contract = self._read(key) if self._fetch_one else None
            self._by_table[key] = (self._clock(), contract)
            self._all_at = None
        return contract

    def resolve(self, file_name: str, version: str | None = None) -> ContractMatch | None:
        """The contract for a landed file: by its table folder when it has one (that contract
        only), else by matching its base name against every contract's file_pattern."""
        basename = file_name.rsplit("/", 1)[-1]
        key = table_of(file_name)
        if key is not None and self._fetch_one is not None:
            contract = self.for_table(key, version)
            if contract is None:
                return None
            return ContractMatch(contract, contract.compiled_pattern.fullmatch(basename))
        matches = [
            ContractMatch(c, m) for c in self.contracts() if (m := c.compiled_pattern.fullmatch(basename))
        ]
        if len(matches) > 1:
            names = ", ".join(m.contract.name for m in matches)
            raise ContractError(f"File {basename!r} matches multiple contracts: {names}")
        return matches[0] if matches else None
