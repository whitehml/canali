# Contributing

## Development environment

Prerequisites: [uv](https://docs.astral.sh/uv/) and Python 3.12.

```
uv sync --all-packages      # create .venv, install every module and the dev group
uv run pre-commit install   # write .git/hooks/pre-commit
```

The first commit after installing builds the hook environments, which repeats only when `.pre-commit-config.yaml`
changes.

## Pre-commit checks

```
uv run ruff check .         # lint
uv run ruff format .        # format
uv run mypy                 # types, strict
uv run pytest               # tests
```

The pre-commit hooks will run all four. Tests run by the hooks must not require database access.

## Workspace

The repository is a single uv workspace. The root `pyproject.toml` holds the shared ruff, mypy and pytest configuration
and is not itself a package. Each module carries its own dependencies, scripts and overrides.

### Ruff

A module without its own `[tool.ruff]` inherits the root configuration. A module that needs an override writes
`extend = "../pyproject.toml"` to selectively overwrite.

```toml
# Root pyproject.toml
[tool.ruff]
line-length = 120
target-version = "py312"
```

```toml
# epa/pyproject.toml
[tool.ruff]
extend = "../pyproject.toml"

# The update rule reads better with the model's own names: K, M, N.
[tool.ruff.lint.per-file-ignores]
"src/epa/model.py" = ["N803", "N806"]
```

### Mypy

All Mypy overrides, `[[tool.mypy.overrides]]`, are declared at the root regardless of the module they apply to.

```toml
# Root pyproject.toml
[tool.mypy]
python_version = "3.12"
strict = true

# This override lives at the root but targets a single module's dependency.
[[tool.mypy.overrides]]
module = ["pgserver.*"]
ignore_missing_imports = true
```

## Test tags

Untagged tests run everywhere. `pytest` deselects `credentialed`, `scheduled` and `db` by default;
`--strict-markers` rejects any tag not declared at the root.

| Tag | Meaning |
| --- | --- |
| `credentialed` | needs network access or credentials; not run in CI |
| `scheduled` | regular checks for data accuracy/calibrations |
| `db` | needs a live Postgres |

## Style

Docstrings: [Google style](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings).

Inline comments: only where the implementation works around a language feature or is genuinely counter-intuitive. Code
should avoid abbreviations.

Documentation: a reference to what is, not why. Treat documentation like your favorite TTRPG index layout: a reference
manual to get in and out of quickly. Methodology documents may cover why, naming the specific A/B tests and experiment
references behind a decision, while still leading with what.

Imports: absolute. Relative imports to a parent package are banned by lint.

Line length: 120, and the Python target version is 3.12, both set once at the root.

Tests: non-brittle, except where brittleness is the point. Data validation and error-metric tripwires are the narrow
exception.

Markdown: GitHub [alerts](https://marketplace.visualstudio.com/items?itemName=kejun.markdown-alert) and
[footnotes](https://marketplace.visualstudio.com/items?itemName=bierner.markdown-footnotes) are in use. The linked
VS Code plugins render them locally.
