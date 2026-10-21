"""Constants every model reading the warehouse shares."""

from __future__ import annotations

# Alliance-rows, in event-sequential order, before a season's own data replaces what earlier seasons provide.
INIT_WINDOW = 4000

QUALIFICATION_MATCHES: tuple[str, ...] = ("QUALIFICATION",)
ELIMINATION_MATCHES: tuple[str, ...] = ("SEMIFINAL", "FINAL", "PLAYOFF")
