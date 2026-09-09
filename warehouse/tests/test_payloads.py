"""The content-addressed raw payload store."""

from __future__ import annotations

import gzip
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from warehouse.ingest.payloads import PayloadStore
from warehouse.schema.raw import raw_payload

BODY = b'{"events": [{"code": "USPAX"}]}'


def test_a_body_round_trips_through_the_store(tmp_path: Path) -> None:
    store = PayloadStore(tmp_path)
    stored = store.put(BODY)

    assert stored.payload_hash == hashlib.sha256(BODY).hexdigest()
    assert stored.byte_length == len(BODY)
    assert stored.newly_written
    assert store.get(stored.payload_hash) == BODY


def test_a_body_is_stored_gzipped(tmp_path: Path) -> None:
    stored = PayloadStore(tmp_path).put(BODY)

    assert gzip.decompress(stored.path.read_bytes()) == BODY


def test_payloads_fan_out_rather_than_filling_one_directory(tmp_path: Path) -> None:
    store = PayloadStore(tmp_path)
    stored = [store.put(b'{"n": %d}' % n) for n in range(64)]

    assert len({s.path.parent for s in stored}) >= 60
    assert all(store.get(s.payload_hash) == b'{"n": %d}' % n for n, s in enumerate(stored))


def test_storing_the_same_body_twice_writes_nothing_new(tmp_path: Path) -> None:
    store = PayloadStore(tmp_path)
    first = store.put(BODY)
    inode = first.path.stat().st_ino
    second = store.put(BODY)

    assert second.payload_hash == first.payload_hash
    assert second.newly_written is False
    assert second.path.stat().st_ino == inode
    assert len(list(tmp_path.rglob("*.json.gz"))) == 1


def test_the_gzip_container_carries_no_timestamp(tmp_path: Path) -> None:
    stored = PayloadStore(tmp_path).put(BODY)

    assert stored.path.read_bytes()[4:8] == b"\x00\x00\x00\x00"
    assert PayloadStore(tmp_path / "elsewhere").put(BODY).path.read_bytes() == stored.path.read_bytes()


def test_a_revised_body_does_not_displace_the_one_it_revises(tmp_path: Path) -> None:
    store = PayloadStore(tmp_path)
    first = store.put(BODY)
    second = store.put(BODY.replace(b"USPAX", b"USPAY"))

    assert second.payload_hash != first.payload_hash
    assert store.get(first.payload_hash) == BODY


def test_no_scratch_file_survives_a_write(tmp_path: Path) -> None:
    store = PayloadStore(tmp_path)
    store.put(BODY)

    assert list(tmp_path.rglob("*.tmp")) == []


def test_a_body_the_store_does_not_hold_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        PayloadStore(tmp_path).get("0" * 64)


@pytest.mark.db
def test_recording_a_stored_body_is_idempotent(clean_engine: Engine, tmp_path: Path) -> None:
    store = PayloadStore(tmp_path)
    stored = store.put(BODY)
    fetched_at = datetime(2026, 3, 15, 19, 52, 21, tzinfo=UTC)

    with clean_engine.begin() as conn:
        for _ in range(2):
            store.record(conn, stored, endpoint="/2025/events", last_modified="x", fetched_at_utc=fetched_at)
        rows = conn.execute(select(raw_payload)).mappings().all()

    assert len(rows) == 1
    assert rows[0]["payload_hash"] == stored.payload_hash
    assert rows[0]["byte_length"] == len(BODY)
    assert rows[0]["path"] == str(stored.path.relative_to(tmp_path))
