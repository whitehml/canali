"""Exercise the actual checkpoint SQL against SQLite's transactional semantics."""

import sqlite3
from pathlib import Path

from forecast_store import INITIALIZE, READ_STATE, SAVE_STATE


def test_publication_rejects_duplicate_steps_old_inputs_and_old_engines():
    db = sqlite3.connect(":memory:")
    for migration in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
        db.executescript(migration.read_text())
    db.execute("INSERT INTO forecast_input VALUES ('TEST', 1, '{}')")
    db.execute(INITIALIZE, ("engine-a", "TEST", 1))
    args = ("{}", "running", "{}", "TEST", 1, "engine-a", 0, "TEST", 1)
    assert db.execute(SAVE_STATE, args).fetchone() == (1,)
    assert db.execute(SAVE_STATE, args).fetchone() is None
    db.execute("UPDATE forecast_input SET revision = 2")
    assert db.execute(READ_STATE, ("TEST", "engine-a")).fetchone() is None
    assert db.execute(SAVE_STATE, (*args[:6], 1, "TEST", 1)).fetchone() is None
    db.execute(INITIALIZE, ("engine-a", "TEST", 2))
    # A request that loaded revision 1 cannot reinitialize the revision-2 checkpoint.
    db.execute(INITIALIZE, ("engine-a", "TEST", 1))
    assert db.execute("SELECT input_revision, step FROM forecast_state").fetchone() == (2, 0)
    db.execute(INITIALIZE, ("engine-b", "TEST", 2))
    assert db.execute(SAVE_STATE, ("{}", "complete", "{}", "TEST", 2, "engine-a", 0, "TEST", 2)).fetchone() is None
    assert db.execute(READ_STATE, ("TEST", "engine-a")).fetchone() is None
    assert db.execute(READ_STATE, ("TEST", "engine-b")).fetchone() is not None
