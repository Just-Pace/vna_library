# src/vna_library/e5071c.py
"""
Driver for the Keysight/Agilent E5071C (classic ENA family).

Implements the `VNA` interface (see base.py) with classic ENA SCPI:
trace handling via CALC:PAR:DEF, bulk limit loading via CALC:LIM:DATA,
and a per-channel marker search domain.

Driver extras beyond the VNA interface:
  * fetch_fdata_pairs()  - (primary, secondary) value pairs per point
  * query_limit_table()  - read back the loaded limit table
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .base import (
    ScpiVNA, LimitSeg,
    VNAError, InstrumentError, VNATimeout,  # re-exported for back-compat
)
from .models import MarkerFunc

__all__ = ["E5071C", "VNAError", "InstrumentError", "VNATimeout"]


class E5071C(ScpiVNA):
    def _reset_bookkeeping(self) -> None:
        self._marker_domain: dict[int, tuple[float, float]] = {}  # ch -> (start, stop)

    # --- traces ---------------------------------------------------------------
    def define_trace(self, trace, ch: int = 1, window: int = 1, trace_num: int = 1) -> None:
        self.write(f'CALC{ch}:PAR:DEF "{trace.name}","{trace.sparam}"')
        self.write(f'DISP:WIND{window}:TRAC{trace_num}:FEED "{trace.name}"')
        self.write(f"CALC{ch}:FORM {trace.fmt}")

    def define_equation(self, eq, ch: int = 1, window: int = 1, trace_num: int = 3) -> None:
        self.write(f'CALC{ch}:PAR:DEF "{eq.name}",EQU')
        self.write(f'CALC{ch}:PAR:SEL "{eq.name}"')
        self.write(f'CALC{ch}:EQU:DEF "{eq.expression}"')
        self.write(f'DISP:WIND{window}:TRAC{trace_num}:FEED "{eq.name}"')
        self.write(f"CALC{ch}:EQU:STAT ON")

    # --- limits ---------------------------------------------------------------
    def limits_clear(self, ch: int = 1) -> None:
        self.write(f"CALC{ch}:LIM:DEL:ALL")

    def _write_limit_segments(self, segments: List[LimitSeg], ch: int = 1) -> None:
        """Bulk-load via :CALC{ch}:LIM:DATA exactly as the E5071C expects:

          :CALC1:LIM:DATA <num>,<type1>,<fstart1>,<fstop1>,<ystart1>,<ystop1>,...

        type: 1=MAX, 2=MIN; frequencies in Hz; Y units match the currently
        selected format (dB, dBm, ...).
        """
        flat: list[str] = [str(len(segments))]
        for typ, fs, fe, ys, ye in segments:
            # Keep formatting "boring" (no separators/units); .12g is safe
            # for SCPI numeric parsing.
            flat.append(str(int(typ)))
            flat.append(f"{float(fs):.12g}")
            flat.append(f"{float(fe):.12g}")
            flat.append(f"{float(ys):.12g}")
            flat.append(f"{float(ye):.12g}")
        self.write(f"CALC{ch}:LIM:DATA {','.join(flat)}")

    def query_limit_table(self, ch: int = 1) -> List[LimitSeg]:
        """Query and parse the current limit table (driver extra).

        Returns:
            Limit segments as (type, f_start_hz, f_stop_hz, y_start, y_stop)
            tuples; empty list if no limits are defined.
        """
        self.write("FORM:DATA ASC")
        values = self.query_values(f"CALC{ch}:LIM:DATA?")
        if not values:
            return []
        count = int(values[0])
        if count == 0:
            return []
        segments = []
        for i in range(count):
            idx = 1 + i * 5
            if idx + 4 >= len(values):
                break
            segments.append((
                int(values[idx]),
                float(values[idx + 1]),
                float(values[idx + 2]),
                float(values[idx + 3]),
                float(values[idx + 4]),
            ))
        return segments

    # --- sweep & data -----------------------------------------------------------
    def single_sweep(self, ch: int = 1) -> None:
        self.write("INIT:CONT OFF")
        self.write("*TRG")
        self.opc()

    def fetch_fdata_pairs(self, ch: int = 1) -> List[Tuple[float, float]]:
        """(primary, secondary) formatted value pairs per point (driver extra)."""
        self.write("FORM:DATA ASC")
        raw = self.query_values(f"CALC{ch}:DATA:FDATA?")
        return list(zip(raw[0::2], raw[1::2]))

    def fetch_fdata(self, ch: int = 1) -> List[float]:
        return [pair[0] for pair in self.fetch_fdata_pairs(ch=ch)]

    def fetch_stimulus(self, ch: int = 1) -> List[float]:
        self.write("FORM:DATA ASC")
        return self.query_values(f"SENS{ch}:FREQ:DATA?")

    # --- markers ----------------------------------------------------------------
    def marker_set_function(self, idx: int, func: MarkerFunc, ch: int = 1) -> None:
        """Set the marker search function type and execute the search."""
        self.write(f"CALC{ch}:MARK{idx}:FUNC:TYPE {func.value}")
        self.write(f"CALC{ch}:MARK{idx}:FUNC:EXEC")

    def marker_set_range(self, f_start_hz: float, f_stop_hz: float,
                         ch: int = 1, *, marker: Optional[int] = None) -> None:
        """Set the marker search domain range and enable it.

        The search domain range is per-channel on the E5071C, not
        per-marker, so `marker` is accepted for interface compatibility
        but ignored.  All markers on a channel share this range.  Calling
        this with a different band on a channel that already has one set
        raises InstrumentError - call marker_clear_domain(ch) first.
        """
        self._validate_band(f_start_hz, f_stop_hz)
        band = (f_start_hz, f_stop_hz)
        prev = self._marker_domain.get(ch)
        if prev is not None and prev != band:
            raise InstrumentError(
                f"Channel {ch} marker domain already set to "
                f"{prev[0]:.6g}–{prev[1]:.6g} Hz. "
                f"Call marker_clear_domain({ch}) before changing band."
            )
        self.write(f"CALC{ch}:MARK:FUNC:DOM:STAR {f_start_hz:.12g}")
        self.write(f"CALC{ch}:MARK:FUNC:DOM:STOP {f_stop_hz:.12g}")
        self.write(f"CALC{ch}:MARK:FUNC:DOM ON")
        self._marker_domain[ch] = band

    def marker_clear_domain(self, ch: int = 1, *, marker: Optional[int] = None) -> None:
        """Clear the per-channel marker search domain (`marker` is ignored).

        Must be called before marker_set_range when changing the band on a
        channel that already has a domain set.
        """
        self.write(f"CALC{ch}:MARK:FUNC:DOM OFF")
        self._marker_domain.pop(ch, None)
