# presets/limits.py
"""
Reusable limit-table presets for common VNA pass/fail envelopes.

These builders return :class:`LimitTable` objects assembled from the
``vna_library`` models, so recipes can apply consistent limits without
hand-rolling segments.  Every builder is generic over frequency — pass the
band you care about and reuse it anywhere.

Convention (matches E5071C log-mag traces):
  * S-parameter limits are expressed in dB.
  * ``LimitType.MAX`` means the trace must stay at or below the line.
  * ``LimitType.MIN`` means the trace must stay at or above the line.
"""
from __future__ import annotations

from typing import Iterable, Tuple

from vna_library import LimitTable, LimitSegment, LimitType


def flat_max(f_start_hz: float, f_stop_hz: float, y_db: float) -> LimitSegment:
    """One flat MAX segment: trace must stay <= ``y_db`` across the band."""
    return LimitSegment(
        kind=LimitType.MAX,
        f_start_hz=f_start_hz,
        f_stop_hz=f_stop_hz,
        y_start_db=y_db,
        y_stop_db=y_db,
    )


def flat_min(f_start_hz: float, f_stop_hz: float, y_db: float) -> LimitSegment:
    """One flat MIN segment: trace must stay >= ``y_db`` across the band."""
    return LimitSegment(
        kind=LimitType.MIN,
        f_start_hz=f_start_hz,
        f_stop_hz=f_stop_hz,
        y_start_db=y_db,
        y_stop_db=y_db,
    )


def return_loss_envelope(
    f_start_hz: float,
    f_stop_hz: float,
    max_db: float = -10.0,
) -> LimitTable:
    """Return-loss envelope for an S11 log-mag trace.

    Enforces ``|S11| <= max_db`` across ``[f_start_hz, f_stop_hz]``.  The
    default of -10 dB corresponds to roughly a 2:1 VSWR match.

    Args:
        f_start_hz: Band start frequency in Hz.
        f_stop_hz: Band stop frequency in Hz.
        max_db: Worst acceptable return loss in dB (negative, e.g. -10).

    Returns:
        A single-segment :class:`LimitTable`.
    """
    return LimitTable.from_iter([flat_max(f_start_hz, f_stop_hz, max_db)])


def multiband_return_loss(
    bands: Iterable[Tuple[float, float]],
    max_db: float = -10.0,
) -> LimitTable:
    """Return-loss envelope spanning several bands with the same limit.

    Args:
        bands: Iterable of ``(f_start_hz, f_stop_hz)`` tuples.
        max_db: Worst acceptable return loss in dB applied to every band.

    Returns:
        A :class:`LimitTable` with one MAX segment per band.

    Raises:
        ValueError: If ``bands`` is empty.
    """
    segments = [flat_max(f0, f1, max_db) for f0, f1 in bands]
    if not segments:
        raise ValueError("bands is empty")
    return LimitTable.from_iter(segments)
