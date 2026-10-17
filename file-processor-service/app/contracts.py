"""Load data contracts (YAML) and resolve a file name to its contract."""

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


class ContractError(Exception):
    """Contract configuration problem (permanent; retrying won't help)."""


@dataclass(frozen=True)
class ContractMatch:
    contract: Contract
    match: re.Match[str]

    @property
    def file_date(self) -> date | None:
        return self.contract.file_date(self.match)


def gcs_source(client: storage.Client, bucket: str, prefix: str) -> ContractSource:
    def load() -> dict[str, str]:
        return {
            blob.name: blob.download_as_text()
            for blob in client.list_blobs(bucket, prefix=prefix)
            if blob.name.endswith((".yaml", ".yml"))
        }

    return load


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


class ContractRegistry:
    """Caches contracts for `ttl_seconds` so edits in the config bucket are picked up without a redeploy."""

    def __init__(
        self,
        source: ContractSource,
        ttl_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._contracts: list[Contract] = []
        self._loaded_at: float | None = None

    def contracts(self) -> list[Contract]:
        with self._lock:
            now = self._clock()
            if self._loaded_at is None or now - self._loaded_at >= self._ttl:
                self._contracts = parse_contracts(self._source())
                self._loaded_at = now
                log.info(
                    "Loaded %d contracts: %s",
                    len(self._contracts),
                    ", ".join(f"{c.name} v{c.version} ({c.file_pattern})" for c in self._contracts),
                )
            return self._contracts

    def resolve(self, file_name: str) -> ContractMatch | None:
        """Match the file's base name (any folder prefix is ignored) against each contract's pattern."""
        basename = file_name.rsplit("/", 1)[-1]
        matches = [
            ContractMatch(c, m) for c in self.contracts() if (m := c.compiled_pattern.fullmatch(basename))
        ]
        if len(matches) > 1:
            names = ", ".join(m.contract.name for m in matches)
            raise ContractError(f"File {basename!r} matches multiple contracts: {names}")
        return matches[0] if matches else None
