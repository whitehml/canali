"""Bundle the existing numerical kernels without the Postgres/CLI dependencies."""

import hashlib
from pathlib import Path
from shutil import copyfile

ROOT = Path(__file__).resolve().parent
MODULES = {
    "epa": ["model.py", "constants.py", "corpus.py", "evaluate.py", "norm.py", "partition.py", "replay.py", "scale.py"],
    "pridge": ["estimator.py"],
    "sim": [
        "score.py",
        "event.py",
        "runner.py",
        "convergence.py",
        "match.py",
        "schedule.py",
        "standings.py",
        "selection.py",
        "playoff.py",
        "advancement.py",
        "awards.py",
        "award_model.py",
        "pick_model.py",
        "conditional_logit.py",
        "rp_probability.py",
        "rp_source.py",
    ],
    "warehouse": ["constants.py", "tier.py", "rules/__init__.py", "rules/model.py", "rules/partition.py"],
}
digest = hashlib.sha256()
for package, modules in MODULES.items():
    source = ROOT.parent / package / "src" / package
    target = ROOT / "src" / package
    target.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", *modules):
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        if not (target / name).exists() or (target / name).read_bytes() != (source / name).read_bytes():
            copyfile(source / name, target / name)
        digest.update(f"{package}/{name}".encode())
        digest.update((source / name).read_bytes())
for name in (
    "forecast.py",
    "snapshot.py",
    "forecast_store.py",
    "season.py",
    "first_event.py",
    "components.py",
    "assemble.py",
    "playoff_input.py",
):
    path = ROOT / "src" / name
    if path.exists():
        digest.update(path.read_bytes())
digest.update((ROOT / "pylock.toml").read_bytes())
version_file = ROOT / "src" / "engine_version.py"
version_source = f'ENGINE_VERSION = "{digest.hexdigest()}"\n'
if not version_file.exists() or version_file.read_text(encoding="utf-8") != version_source:
    version_file.write_text(version_source, encoding="utf-8")
