"""Serialize static deployments and retain the last successfully published asset set."""

import hashlib
import json
import os
import shutil
import subprocess
from contextlib import contextmanager

from prepare import ROOT, write_json

STATE = ROOT.parent / "var" / "forecast-publication"


def _lock_file(lock, *, release=False):
    lock.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def publication_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.write(b"0")
        lock.flush()
        try:
            _lock_file(lock)
        except OSError as exc:
            raise RuntimeError("another static publisher is already running") from exc
        try:
            yield
        finally:
            _lock_file(lock, release=True)


def prepare_site(site):
    # Data lives separately from the compiled application; retain old immutable assets.
    output = site / "generated"
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "public" / "_headers", site / "_headers")
    return output


def deploy_if_changed(site, state, *, run=subprocess.run):
    digest = hashlib.sha256((ROOT / "data.wrangler.toml").read_bytes())
    for path in sorted(site.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(site).as_posix().encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    fingerprint = digest.hexdigest()
    if state.exists() and json.loads(state.read_text(encoding="utf-8"))["fingerprint"] == fingerprint:
        return False
    run(
        [
            "node",
            str(ROOT.parent / "worker/node_modules/wrangler/bin/wrangler.js"),
            "deploy",
            "--config",
            str(ROOT / "data.wrangler.toml"),
            "--assets",
            str(site.resolve()),
        ],
        cwd=ROOT,
        check=True,
    )
    write_json(state, {"fingerprint": fingerprint})
    return True
