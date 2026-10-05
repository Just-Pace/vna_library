# src/vna_library/base.py
"""
Instrument-agnostic VNA interface and shared SCPI plumbing.

Architecture:

    VNA (ABC)          -- the contract recipes program against
      |
    ScpiVNA(VNA)       -- pyvisa transport + behaviour shared by every
      |                   SCPI VNA (validation, orchestration, bookkeeping)
      +-- E5071C       -- classic Keysight ENA syntax   (e5071c.py)
      +-- PNAFamily    -- modern Keysight platform      (pna_family.py)

Recipes should type-annotate against `VNA` and obtain instances through
`vna_library.connect()` (see factory.py), which dispatches on *IDN?.
Drivers may expose extras beyond the interface (e.g. E5071C's
``fetch_fdata_pairs``); recipes that use extras tie themselves to that
driver.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import List, Optional, Sequence, Tuple

from .models import (
    SweepConfig, TraceSpec, EquationSpec,
    LimitTable, MarkerSpec, MarkerFunc,
)

# (type, f_start_hz, f_stop_hz, y_start, y_stop) -- type: 1=MAX, 2=MIN
LimitSeg = Tuple[int, float, float, float, float]


class VNAError(Exception): ...
class InstrumentError(VNAError): ...
class VNATimeout(VNAError): ...


class VNA(ABC):
    """Abstract VNA: the minimal contract an instrument driver must honor.

    Semantics every driver guarantees:

    * Trace-scoped operations (format, limits, markers, data fetch) act on
      the *selected* trace/measurement of the channel.  Call
      :meth:`select_trace` first to ensure correct binding.
    * ``apply_limit_table_bulk`` loads the whole table in one call and can
      enable display + testing.
    * ``marker_configure`` orchestrates enable -> search range -> search
      function for one :class:`MarkerSpec`.  Whether the search range is
      per-channel or per-marker is driver-specific (E5071C: per-channel;
      PNA family: per-marker user ranges) -- portable recipes should not
      assume overlapping per-marker bands work everywhere.
    """

    # --- identity & sync ---------------------------------------------------
    @abstractmethod
    def idn(self) -> str: ...
    @abstractmethod
    def preset(self) -> None: ...
    @abstractmethod
    def reset(self) -> None: ...
    @abstractmethod
    def opc(self) -> None: ...
    @abstractmethod
    def get_errors(self) -> List[str]: ...

    # --- configuration -----------------------------------------------------
    @abstractmethod
    def apply_sweep(self, sweep: SweepConfig, ch: int = 1) -> None: ...
    @abstractmethod
    def clear_traces(self, ch: int = 1) -> None: ...
    @abstractmethod
    def define_trace(self, trace: TraceSpec, ch: int = 1,
                     window: int = 1, trace_num: int = 1) -> None: ...
    @abstractmethod
    def define_equation(self, eq: EquationSpec, ch: int = 1,
                        window: int = 1, trace_num: int = 3) -> None: ...
    @abstractmethod
    def select_trace(self, trace_name: str, ch: int = 1) -> None: ...

    # --- limits ------------------------------------------------------------
    @abstractmethod
    def limits_on(self, ch: int = 1) -> None: ...
    @abstractmethod
    def limits_clear(self, ch: int = 1) -> None: ...
    @abstractmethod
    def apply_limit_table_bulk(
        self, table: LimitTable, ch: int = 1,
        meas_name: Optional[str] = None, clear_first: bool = True,
        enable_display: bool = True, enable_test: bool = True,
        check_errors: bool = True,
    ) -> None: ...

    # --- sweep & data ------------------------------------------------------
    @abstractmethod
    def single_sweep(self, ch: int = 1) -> None: ...
    @abstractmethod
    def fetch_fdata(self, ch: int = 1) -> List[float]:
        """Formatted response values of the selected trace, one per point."""
    @abstractmethod
    def fetch_stimulus(self, ch: int = 1) -> List[float]:
        """Stimulus (frequency, Hz) values, one per point."""

    # --- markers -----------------------------------------------------------
    @abstractmethod
    def marker_enable(self, idx: int, ch: int = 1, on: bool = True) -> None: ...
    @abstractmethod
    def marker_set_function(self, idx: int, func: MarkerFunc, ch: int = 1) -> None: ...
    @abstractmethod
    def marker_set_range(self, f_start_hz: float, f_stop_hz: float,
                         ch: int = 1, *, marker: Optional[int] = None) -> None: ...
    @abstractmethod
    def marker_clear_domain(self, ch: int = 1, *, marker: Optional[int] = None) -> None: ...
    @abstractmethod
    def marker_configure(self, spec: MarkerSpec, ch: int = 1) -> None: ...
    @abstractmethod
    def marker_read(self, idx: int, ch: int = 1) -> Tuple[float, float]: ...


class ScpiVNA(VNA):
    """Shared SCPI transport + the behaviour common to all SCPI VNAs.

    Accepts either a VISA resource string (opened via pyvisa, imported
    lazily so the package works without pyvisa for offline tests) or an
    already-open session object exposing ``write`` / ``query`` /
    ``query_ascii_values`` (used by the factory and by tests).
    """

    def __init__(self, resource, timeout_ms: int = 30000):
        if hasattr(resource, "query") and hasattr(resource, "write"):
            self.inst = resource  # adopt a pre-configured session as-is
        else:
            import pyvisa
            rm = pyvisa.ResourceManager()
            self.inst = rm.open_resource(resource)
            self.inst.timeout = timeout_ms
            self.inst.write_termination = "\n"
            self.inst.read_termination = "\n"
        self._reset_bookkeeping()

    def _reset_bookkeeping(self) -> None:
        """Clear driver-side state; called on construction, preset, reset."""

    # --- low-level ----------------------------------------------------------
    def write(self, cmd: str) -> None:
        self.inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self.inst.query(cmd)

    def query_values(self, cmd: str) -> List[float]:
        return self.inst.query_ascii_values(cmd)

    def opc(self) -> None:
        self.query("*OPC?")

    def idn(self) -> str:
        return self.query("*IDN?").strip()

    def preset(self) -> None:
        self.write("SYST:PRES")
        self._reset_bookkeeping()

    def reset(self) -> None:
        self.write("*RST")
        self._reset_bookkeeping()

    def get_errors(self) -> List[str]:
        """Drain the instrument error queue; empty list means no errors.

        Handles both ENA ('+0,...') and PNA-family ('0,"No error"') formats.
        """
        errors: List[str] = []
        while True:
            err = self.query("SYST:ERR?").strip()
            try:
                code = int(err.split(",", 1)[0])
            except ValueError:
                code = None
            if code == 0:
                break
            errors.append(err)
            if code is None:  # unparseable response: don't spin forever
                break
        return errors

    def _raise_on_instrument_error(self, context: str) -> None:
        """Sync, then raise InstrumentError if *ESR? reports an error.

        Checks query/device/execution/command error bits (2-5).
        """
        self.opc()
        esr = int(self.query("*ESR?"))
        if esr & 0b00111100:
            errors = self.get_errors()
            raise InstrumentError(
                f"{context} - instrument error: "
                f"{errors[0] if errors else 'Unknown error'}"
            )

    # --- configuration shared across families --------------------------------
    def apply_sweep(self, sweep: SweepConfig, ch: int = 1) -> None:
        self.write(f"SENS{ch}:FREQ:STAR {sweep.start_hz}")
        self.write(f"SENS{ch}:FREQ:STOP {sweep.stop_hz}")
        self.write(f"SENS{ch}:SWE:POIN {sweep.points}")
        self.write(f"SENS{ch}:BWID {sweep.if_bw_hz}")
        self.write(f"SENS{ch}:AVER:STAT {'ON' if sweep.avg_count > 1 else 'OFF'}")
        if sweep.avg_count > 1:
            self.write(f"SENS{ch}:AVER:COUN {sweep.avg_count}")
        self.write(f"SOUR{ch}:POW {sweep.power_dbm}")

    def clear_traces(self, ch: int = 1) -> None:
        self.write(f"CALC{ch}:PAR:DEL:ALL")

    def select_trace(self, trace_name: str, ch: int = 1) -> None:
        self.write(f'CALC{ch}:PAR:SEL "{trace_name}"')

    # --- limits: shared validation + orchestration ---------------------------
    @staticmethod
    def _validated_segments(table: LimitTable) -> List[LimitSeg]:
        segments: Sequence[LimitSeg] = [
            (int(seg.kind), seg.f_start_hz, seg.f_stop_hz,
             seg.y_start_db, seg.y_stop_db)
            for seg in table.segments
        ]
        if not segments:
            raise ValueError("segments is empty")
        for i, (typ, fs, fe, ys, ye) in enumerate(segments, start=1):
            if typ not in (1, 2):
                raise ValueError(
                    f"segment {i}: typ must be 1 (MAX) or 2 (MIN), got {typ}")
            for name, v in (("fs", fs), ("fe", fe), ("ys", ys), ("ye", ye)):
                if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                    raise ValueError(
                        f"segment {i}: {name} must be a finite number, got {v!r}")
            if fe <= fs:
                raise ValueError(
                    f"segment {i}: f_stop_hz must be > f_start_hz "
                    f"(got {fs} .. {fe})")
        return list(segments)

    def _write_limit_segments(self, segments: List[LimitSeg], ch: int = 1) -> None:
        """Family-specific: load validated segments into the instrument."""
        raise NotImplementedError

    def limits_on(self, ch: int = 1) -> None:
        self.write(f"CALC{ch}:LIM:STAT ON")

    def apply_limit_table_bulk(
        self,
        table: LimitTable,
        ch: int = 1,
        meas_name: Optional[str] = None,
        clear_first: bool = True,
        enable_display: bool = True,
        enable_test: bool = True,
        check_errors: bool = True,
    ) -> None:
        """Load a limit table onto the selected (or named) trace.

        Args:
            table: LimitTable containing segments to apply.
            ch: Channel number.
            meas_name: Optional trace/measurement name to select first.
                If None, uses the currently selected trace.
            clear_first: Clear existing limits before applying.
            enable_display: Enable the limit-line display.
            enable_test: Enable limit testing (pass/fail judgment).
            check_errors: Raise InstrumentError if the instrument reports
                an error after the segments are written.

        Raises:
            ValueError: On empty/invalid segments.
            InstrumentError: If the instrument reports an error
                (only when check_errors=True).
        """
        segments = self._validated_segments(table)
        if meas_name is not None:
            self.select_trace(meas_name, ch=ch)
        if clear_first:
            self.limits_clear(ch=ch)
        self._write_limit_segments(segments, ch=ch)
        if check_errors:
            self._raise_on_instrument_error("Limit table load failed")
        if enable_display:
            self.write(f"CALC{ch}:LIM:DISP ON")
        if enable_test:
            self.limits_on(ch=ch)

    # --- markers: shared pieces ----------------------------------------------
    def marker_enable(self, idx: int, ch: int = 1, on: bool = True) -> None:
        """Enable or disable a marker on the selected trace."""
        self.write(f"CALC{ch}:MARK{idx}:STAT {'ON' if on else 'OFF'}")

    def marker_configure(self, spec: MarkerSpec, ch: int = 1) -> None:
        """Configure a marker from a MarkerSpec.

        Orchestrates: enable -> set search range (if enabled) -> set search
        function + execute.  Does NOT call opc(); configure all markers,
        then sync once::

            vna.marker_configure(spec1)
            vna.marker_configure(spec2)
            vna.opc()

        Warning:
            Markers bind to the currently selected trace.  Call
            select_trace() before marker_configure().
        """
        self.marker_enable(spec.index, ch=ch, on=True)
        if spec.enable_domain:
            self.marker_set_range(spec.f_start_hz, spec.f_stop_hz,
                                  ch=ch, marker=spec.index)
        self.marker_set_function(spec.index, spec.func, ch=ch)

    def marker_read(self, idx: int, ch: int = 1) -> Tuple[float, float]:
        """Read the marker's X (stimulus, Hz) and primary Y value.

        MARK:Y? returns two values on both families; only the primary is
        returned here.  Smith/Polar formats need a dedicated method.

        Warning:
            Values are for the marker on the currently selected trace.
        """
        x = float(self.query(f"CALC{ch}:MARK{idx}:X?").strip())
        parts = self.query(f"CALC{ch}:MARK{idx}:Y?").strip().split(",")
        y = float(parts[0])
        return (x, y)

    @staticmethod
    def _validate_band(f_start_hz: float, f_stop_hz: float) -> None:
        if f_stop_hz <= f_start_hz:
            raise ValueError(
                f"f_stop_hz must be > f_start_hz (got {f_start_hz} .. {f_stop_hz})"
            )
