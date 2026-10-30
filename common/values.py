"""Dependency-free value helpers shared across the analytic modules.

These helpers only coerce and format plain values; they carry no business
rules, so ``common`` stays free of any ``modules`` import.
"""

from __future__ import annotations

from typing import Any

MONTH_NAMES = [
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def is_num(v: Any) -> bool:
    """Whether ``v`` can be read as a float.

    Args:
        v (Any): Value to test.

    Returns:
        bool: ``True`` when ``float(v)`` succeeds, ``False`` otherwise.

    Example:
        >>> is_num("3.5"), is_num("n/a")
        (True, False)
    """
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def month_label(m: str) -> str:
    """Render a month key as a short human label.

    Args:
        m (str): Month in ``YYYY-MM`` form.

    Returns:
        str: ``'Jun 2025'`` for ``'2025-06'``; the input unchanged when it
        cannot be split or the month number is out of range.

    Example:
        >>> month_label("2025-06")
        'Jun 2025'
    """
    try:
        y, mo = str(m).split("-")[:2]
        return f"{MONTH_NAMES[int(mo)]} {y}"
    except (ValueError, IndexError):
        return str(m)
