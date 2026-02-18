# src/vna_library/models.py
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Iterable, List, Tuple


@dataclass(frozen=True)
class SweepConfig:
    start_hz: float
    stop_hz: float
    points: int = 601
    if_bw_hz: float = 1e3
    power_dbm: float = 0.0
    avg_count: int = 1


@dataclass(frozen=True)
class TraceSpec:
    name: str          # e.g. "Tr1"
    sparam: str        # e.g. "S11"
    fmt: str = "MLOG"  # log magnitude


@dataclass(frozen=True)
class EquationSpec:
    name: str
    expression: str


class LimitType(IntEnum):
    MAX = 1
    MIN = 2


@dataclass(frozen=True)
class LimitSegment:
    kind: LimitType
    f_start_hz: float
    f_stop_hz: float
    y_start_db: float
    y_stop_db: float


@dataclass(frozen=True)
class LimitTable:
    segments: Tuple[LimitSegment, ...]

    @staticmethod
    def from_iter(segments: Iterable[LimitSegment]) -> "LimitTable":
        return LimitTable(tuple(segments))


class MarkerFunc(str, Enum):
    MIN = "MIN"
    MAX = "MAX"


@dataclass(frozen=True)
class MarkerSpec:
    """Marker search specification for E5071C.

    Note: The search domain range (f_start_hz / f_stop_hz) is per-channel
    on the E5071C, not per-marker.  All markers on a channel share one range.
    """
    index: int                          # 1..9 (10 = reference marker)
    f_start_hz: float
    f_stop_hz: float
    func: MarkerFunc = MarkerFunc.MIN
    enable_domain: bool = True
