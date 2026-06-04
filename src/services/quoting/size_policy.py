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
