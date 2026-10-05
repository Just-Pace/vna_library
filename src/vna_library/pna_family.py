# src/vna_library/pna_family.py
"""
Driver for Keysight's modern VNA platform (PNA-family firmware).

Covers the instruments that share the PNA code base and SCPI command set:
E5080A/B (ENA), E5081A (ENA-X), N52xx (PNA/PNA-L/PNA-X), P50xx (USB
Streamline), and M980xA (PXIe).

Key syntax differences from the classic ENA (E5071C):
  * Measurements are created with CALC:PAR:DEF:EXT (not CALC:PAR:DEF)
    and may carry an equation via the equation editor (CALC:EQU).
  * Limit tables are loaded per segment (CALC:LIM:SEGM<n>:...), not as
    one CALC:LIM:DATA bulk list.
  * Marker search ranges are per-marker "user ranges"
    (CALC:MARK<n>:FUNC:DOM:USER <r>), not one per-channel domain, so
    markers on the same channel can search different bands concurrently.
  * A single sweep is taken with SENS:SWE:MODE SINGle.

Caution:
    Written against the published Keysight PNA-family SCPI help; not yet
    exercised against hardware.  Verify command acceptance on your
    firmware (`check_errors=True` paths will surface rejections).
"""
from __future__ import annotations

from typing import List, Optional

from .base import ScpiVNA, LimitSeg
from .models import MarkerFunc

__all__ = ["PNAFamily"]

_LIMIT_TYPE = {1: "MAX", 2: "MIN"}
_SEARCH_FUNC = {MarkerFunc.MIN: "MIN", MarkerFunc.MAX: "MAX"}

# Marker user ranges available per channel (range 0 = full span).
_USER_RANGES = 15


class PNAFamily(ScpiVNA):
    def _reset_bookkeeping(self) -> None:
        # ch -> number of limit segments this driver has written, so
        # limits_clear() can switch exactly those off again.  Segments
        # created outside this driver (front panel / other software) are
        # not tracked; preset the instrument for a known-clean state.
        self._limit_segments: dict[int, int] = {}

    # --- traces ---------------------------------------------------------------
    def define_trace(self, trace, ch: int = 1, window: int = 1, trace_num: int = 1) -> None:
        self.write(f'CALC{ch}:PAR:DEF:EXT "{trace.name}","{trace.sparam}"')
        self.write(f'DISP:WIND{window}:TRAC{trace_num}:FEED "{trace.name}"')
        self.select_trace(trace.name, ch=ch)
        self.write(f"CALC{ch}:FORM {trace.fmt}")

    def define_equation(self, eq, ch: int = 1, window: int = 1, trace_num: int = 3) -> None:
        """Create an equation-editor trace.

        The PNA family has no EQU parameter type; an ordinary measurement
        is created (S11 placeholder) and the equation editor overrides its
        displayed data.  Expression syntax differs from the E5071C editor -
        consult the instrument's Equation Editor help for valid functions.
        """
        self.write(f'CALC{ch}:PAR:DEF:EXT "{eq.name}","S11"')
        self.select_trace(eq.name, ch=ch)
        self.write(f'CALC{ch}:EQU:TEXT "{eq.expression}"')
        self.write(f'DISP:WIND{window}:TRAC{trace_num}:FEED "{eq.name}"')
        self.write(f"CALC{ch}:EQU:STAT ON")

    # --- limits ---------------------------------------------------------------
    def limits_clear(self, ch: int = 1) -> None:
        for seg in range(1, self._limit_segments.get(ch, 0) + 1):
            self.write(f"CALC{ch}:LIM:SEGM{seg}:TYPE OFF")
        self._limit_segments[ch] = 0

    def _write_limit_segments(self, segments: List[LimitSeg], ch: int = 1) -> None:
        """Load segments one at a time via CALC:LIM:SEGM<n>.

        Frequencies in Hz; amplitudes in the units of the selected trace's
        format (dB for log-mag).
        """
        for n, (typ, fs, fe, ys, ye) in enumerate(segments, start=1):
            self.write(f"CALC{ch}:LIM:SEGM{n}:TYPE {_LIMIT_TYPE[typ]}")
            self.write(f"CALC{ch}:LIM:SEGM{n}:STIM:STAR {float(fs):.12g}")
            self.write(f"CALC{ch}:LIM:SEGM{n}:STIM:STOP {float(fe):.12g}")
            self.write(f"CALC{ch}:LIM:SEGM{n}:AMPL:STAR {float(ys):.12g}")
            self.write(f"CALC{ch}:LIM:SEGM{n}:AMPL:STOP {float(ye):.12g}")
        self._limit_segments[ch] = max(self._limit_segments.get(ch, 0), len(segments))

    # --- sweep & data -----------------------------------------------------------
    def single_sweep(self, ch: int = 1) -> None:
        self.write(f"SENS{ch}:SWE:MODE SING")
        self.opc()

    def fetch_fdata(self, ch: int = 1) -> List[float]:
        self.write("FORM:DATA ASC,0")
        return self.query_values(f"CALC{ch}:DATA? FDATA")

    def fetch_stimulus(self, ch: int = 1) -> List[float]:
        self.write("FORM:DATA ASC,0")
        return self.query_values(f"CALC{ch}:X?")

    # --- markers ----------------------------------------------------------------
    def marker_set_function(self, idx: int, func: MarkerFunc, ch: int = 1) -> None:
        """Execute a min/max search for the marker on the selected trace."""
        self.write(f"CALC{ch}:MARK{idx}:FUNC:EXEC {_SEARCH_FUNC[func]}")

    def marker_set_range(self, f_start_hz: float, f_stop_hz: float,
                         ch: int = 1, *, marker: Optional[int] = None) -> None:
        """Assign a per-marker search range (user range).

        Unlike the E5071C's per-channel domain, each marker gets its own
        user range (range number = marker number), so markers on one
        channel can search different bands concurrently.

        Args:
            f_start_hz / f_stop_hz: Band edges in Hz.
            ch: Channel number.
            marker: Marker number (1..15); required on this family.

        Raises:
            ValueError: If f_stop_hz <= f_start_hz, marker is None, or
                marker exceeds the available user ranges.
        """
        self._validate_band(f_start_hz, f_stop_hz)
        if marker is None:
            raise ValueError(
                "PNAFamily search ranges are per-marker: pass marker=<n> "
                "(marker_configure() does this automatically)"
            )
        if not 1 <= marker <= _USER_RANGES:
            raise ValueError(
                f"marker must be 1..{_USER_RANGES} to map onto a user range, "
                f"got {marker}"
            )
        self.write(f"CALC{ch}:MARK{marker}:FUNC:DOM:USER {marker}")
        self.write(f"CALC{ch}:MARK{marker}:FUNC:DOM:USER:STAR {f_start_hz:.12g}")
        self.write(f"CALC{ch}:MARK{marker}:FUNC:DOM:USER:STOP {f_stop_hz:.12g}")

    def marker_clear_domain(self, ch: int = 1, *, marker: Optional[int] = None) -> None:
        """Return marker(s) to full-span search (user range 0).

        Args:
            ch: Channel number.
            marker: Marker to reset; None resets markers 1..9.
        """
        markers = [marker] if marker is not None else range(1, 10)
        for m in markers:
            self.write(f"CALC{ch}:MARK{m}:FUNC:DOM:USER 0")
