# @covers AC-004, AC-011, AC-026, AC-036, AC-075
"""Exit codes and error types shared by all phases.

Exit codes (§7): 0 ok / 1 general / 2 config or argument / 3 some files failed /
4 cost limit exceeded.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_PARTIAL = 3
EXIT_COST = 4

# @assumption AS-024
_PRIORITY = {EXIT_COST: 0, EXIT_CONFIG: 1, EXIT_ERROR: 2, EXIT_PARTIAL: 3, EXIT_OK: 4}


def worst(*codes: int) -> int:
    """Combine exit codes by the priority 4 > 2 > 1 > 3 > 0."""
    return min(codes, key=lambda c: _PRIORITY.get(c, 2)) if codes else EXIT_OK


class DnError(Exception):
    """An error that terminates the command with a specific exit code."""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class ConfigError(DnError):
    exit_code = EXIT_CONFIG


class CostLimitExceeded(DnError):
    exit_code = EXIT_COST
