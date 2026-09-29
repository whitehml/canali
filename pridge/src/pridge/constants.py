"""Versioned pRidge constants, derived in `docs/methodology.md`.

`MODEL_VERSION` names a set of ratings that may be compared with one another. Bump it whenever a rating computed now
would not be comparable to one already stored under that name, whether the cause is a constant here or a code path. Do
not bump it for a change of prior source, since every fit run records which prior it used.
"""

MODEL_VERSION = "pridge-0.3.0"

FIXED_LAMBDA = 1.32
