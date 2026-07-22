# src/vna_library/models.py
from __future__ import annotations
import math
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Iterable, Tuple


def _require_finite(name: str, value: float) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number, got {value!r}")


@dataclass(frozen=True)
class SweepConfig:
    start_hz: float
    stop_hz: float
    points: int = 601
    if_bw_hz: float = 1e3
    power_dbm: float = 0.0
    avg_count: int = 1

    def __post_init__(self) -> None:
        _require_finite("start_hz", self.start_hz)
        _require_finite("stop_hz", self.stop_hz)
        _require_finite("if_bw_hz", self.if_bw_hz)
        _require_finite("power_dbm", self.power_dbm)
        if self.stop_hz <= self.start_hz:
            raise ValueError(f"stop_hz must be > start_hz (got {self.start_hz} .. {self.stop_hz})")
        if self.points < 2:
            raise ValueError(f"points must be >= 2, got {self.points}")
        if self.if_bw_hz <= 0:
            raise ValueError(f"if_bw_hz must be > 0, got {self.if_bw_hz}")
        if self.avg_count < 1:
            raise ValueError(f"avg_count must be >= 1, got {self.avg_count}")


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

    def __post_init__(self) -> None:
        if self.kind not in (LimitType.MAX, LimitType.MIN):
            raise ValueError(f"kind must be LimitType.MAX or LimitType.MIN, got {self.kind!r}")
        for name in ("f_start_hz", "f_stop_hz", "y_start_db", "y_stop_db"):
            _require_finite(name, getattr(self, name))
        if self.f_stop_hz <= self.f_start_hz:
            raise ValueError(
                f"f_stop_hz must be > f_start_hz (got {self.f_start_hz} .. {self.f_stop_hz})"
            )


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

    def __post_init__(self) -> None:
        if not 1 <= self.index <= 10:
            raise ValueError(f"index must be 1..10, got {self.index}")
        _require_finite("f_start_hz", self.f_start_hz)
        _require_finite("f_stop_hz", self.f_stop_hz)
        if self.enable_domain and self.f_stop_hz <= self.f_start_hz:
            raise ValueError(
                f"f_stop_hz must be > f_start_hz (got {self.f_start_hz} .. {self.f_stop_hz})"
            )
