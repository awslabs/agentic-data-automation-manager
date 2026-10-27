#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The bounds on the statement timeout and row cap an administrator sets per database.

The admin routes refuse values outside them, and the provider refuses a statement
that arrives without both or outside them. There is no default here: the admin
page suggests one.

MAX_TIMEOUT_SECONDS stays below the provider and dispatcher functions' 300-second
timeout, so the database cancels a slow statement before a function is cut off.
MAX_ROW_CAP keeps a result well within Lambda's 6 MB response limit.
"""

MAX_TIMEOUT_SECONDS = 240
MAX_ROW_CAP = 5000
# How many rows a call returns when the agent does not ask for a number, if the
# database's row cap allows that many. The tool description states it.
DEFAULT_PAGE_ROWS = 1000
# Skipping rows is work the database does inside the statement timeout; past this,
# a narrower WHERE clause is the answer, not another page.
MAX_START_ROW = 1_000_000


class LimitError(ValueError):
    """A limit that is missing or out of bounds; its message is safe to show."""


def checked(timeout_seconds, row_cap) -> tuple[int, int]:
    """(timeout_seconds, row_cap) as ints, or LimitError naming what is wrong."""
    return (_bounded(timeout_seconds, "The statement timeout (seconds)", MAX_TIMEOUT_SECONDS),
            _bounded(row_cap, "The row cap", MAX_ROW_CAP))


def within(value, ceiling: int, what: str) -> int:
    """`value` as an int no greater than `ceiling`, or the ceiling when no value is given."""
    return ceiling if value is None else _bounded(value, what, ceiling)


def page(start_row, max_rows, row_cap: int) -> tuple[int, int]:
    """(start_row, rows to return) for one call, or LimitError. max_rows None means
    DEFAULT_PAGE_ROWS, or the row cap when that is lower; it may not exceed the cap."""
    start = 0 if start_row is None else _bounded(start_row, "start_row", MAX_START_ROW, minimum=0)
    if max_rows is None:
        return start, min(DEFAULT_PAGE_ROWS, row_cap)
    return start, _bounded(max_rows, "max_rows", row_cap)


def _bounded(value, what: str, maximum: int, minimum: int = 1) -> int:
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise LimitError(f"{what} is required.")
    try:
        number = int(str(value).strip())
    except ValueError:
        raise LimitError(f"{what} must be a whole number.") from None
    if not minimum <= number <= maximum:
        raise LimitError(f"{what} must be between {minimum} and {maximum}.")
    return number
