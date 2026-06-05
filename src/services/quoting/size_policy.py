"""Quote sizing policy helpers."""

from __future__ import annotations


def clamp_order_size(size: int | float, min_order_size: int, max_order_size: int) -> int:
    size = int(size or 0)
    if size <= 0:
        return 0
    min_size = max(1, int(min_order_size or 1))
    max_size = max(min_size, int(max_order_size or min_size))
    return max(min_size, min(max_size, size))


def late_window_size(size: int, remaining_seconds: float, reduce_size_seconds: float) -> int:
    if remaining_seconds <= reduce_size_seconds:
        return max(0, int(size // 2))
    return int(size or 0)


def repair_size_or_zero(raw_size: int | float, min_order_size: int) -> int:
    """Return a valid close-only repair size or 0 if below live minimum."""
    raw = int(raw_size or 0)
    if raw < max(1, int(min_order_size or 1)):
        return 0
    return raw


def normalize_quote_sizes(
    yes_size: int | float,
    no_size: int | float,
    min_order_size: int,
    allow_round_up: bool = True,
) -> tuple[int, int]:
    """Enforce Polymarket minimum order size on active quote sides."""
    min_size = max(1, int(min_order_size or 1))
    yes = int(yes_size or 0)
    no = int(no_size or 0)
    if allow_round_up:
        yes = min_size if 0 < yes < min_size else yes
        no = min_size if 0 < no < min_size else no
    else:
        yes = 0 if 0 < yes < min_size else yes
        no = 0 if 0 < no < min_size else no
    return yes, no
