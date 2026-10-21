import asyncio
import json

from engine_version import ENGINE_VERSION
from forecast_store import INITIALIZE, export
from test_season import Database, Request


def test_export_pairs_input_and_result_and_hides_obsolete_or_unfinished_snapshots():
    db = Database()
    db.sql.execute("INSERT INTO forecast_input VALUES ('2025-A', 1, ?)", (json.dumps({"seed": 42}),))
    db.sql.execute(INITIALIZE, (ENGINE_VERSION, "2025-A", 1))

    def read(method="GET"):
        return asyncio.run(export(db, Request(method, None), "2025-A"))

    report, status = read()
    assert status == 200 and report["status"] == "running"
    assert report["snapshot"] is None and report["forecast"] is None
    db.sql.execute(
        "UPDATE forecast_state SET status = 'complete', result = ?", (json.dumps({"result": {"teams": {}}}),)
    )
    report, status = read()
    assert status == 200 and report["snapshot"] == {"seed": 42}
    assert report["forecast"] == {"teams": {}} and report["revision"] == 1
    assert read("POST")[1] == 405
    db.sql.execute("UPDATE forecast_state SET engine_version = 'obsolete'")
    assert read()[1] == 404
    db.sql.execute("UPDATE forecast_state SET engine_version = ?", (ENGINE_VERSION,))
    db.sql.execute("UPDATE forecast_input SET revision = 2")
    assert read()[1] == 404
    db.sql.execute(
        "UPDATE forecast_input SET revision = 1, document = ?",
        (json.dumps({"season_prior": {"season": 2025, "revision": 1}}),),
    )
    assert read()[1] == 404
    db.sql.execute(
        "INSERT INTO season_run(season, config, engine_version, scale_dirty) VALUES (2025, '{}', ?, 0)",
        (ENGINE_VERSION,),
    )
    assert read()[1] == 200
    db.sql.execute("UPDATE season_run SET revision = 2")
    assert read()[1] == 404
