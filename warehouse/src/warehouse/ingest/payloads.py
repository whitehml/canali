"""The content-addressed raw payload store.

Bodies live on disk, gzipped and named for the SHA-256 of the response bytes. Each one sits two directories deep,
under the first two characters of that name and then the next two, so that no single directory ever holds every
payload. The database keeps only the hash and the path.
"""

from __future__ import annotations

import gzip
import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import insert

from warehouse.schema.raw import raw_payload


@dataclass(frozen=True, slots=True)
class StoredPayload:
    """A body on disk. ``newly_written`` is false when the store already held it."""

    payload_hash: str
    path: Path
    byte_length: int
    newly_written: bool


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class PayloadStore:
    """Bodies under ``root``, addressed by content."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, payload_hash: str) -> Path:
        return self.root / payload_hash[:2] / payload_hash[2:4] / f"{payload_hash}.json.gz"

    def put(self, body: bytes) -> StoredPayload:
        """Write a body and return its hash. Writing the same body twice is a no-op."""
        payload_hash = digest(body)
        path = self.path_for(payload_hash)
        if path.exists():
            return StoredPayload(payload_hash, path, len(body), newly_written=False)

        path.parent.mkdir(parents=True, exist_ok=True)
        # The pid keeps two backfill passes writing the same body from sharing one scratch file.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        # mtime=0 makes the container as content-addressable as its contents, so a backup re-copies nothing that has
        # not changed. GzipFile does not close a fileobj it was handed, so the raw handle needs its own manager.
        with (
            tmp.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as fh,
        ):
            fh.write(body)
        # Rename last: a reader never sees a partial body, so a killed backfill re-enters safely.
        tmp.replace(path)
        return StoredPayload(payload_hash, path, len(body), newly_written=True)

    def get(self, payload_hash: str) -> bytes:
        with gzip.open(self.path_for(payload_hash), "rb") as fh:
            return fh.read()

    def record(
        self,
        conn: Connection,
        stored: StoredPayload,
        *,
        endpoint: str,
        last_modified: str | None,
        fetched_at_utc: datetime | None = None,
    ) -> None:
        """Register a stored body in ``raw.raw_payload``. Idempotent."""
        stmt = insert(raw_payload).values(
            payload_hash=stored.payload_hash,
            path=str(stored.path.relative_to(self.root)),
            endpoint=endpoint,
            fetched_at_utc=fetched_at_utc or datetime.now(UTC),
            last_modified=last_modified,
            byte_length=stored.byte_length,
        )
        conn.execute(stmt.on_conflict_do_nothing(index_elements=["payload_hash"]))
