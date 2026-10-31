# src/vna_library/pna.py
"""Driver for Keysight PNA-family network analyzers (new NA platform).

Covers the instruments that run Keysight's unified Network Analyzer firmware:
E5080A/B ENA-X, M980xA/M983xA PXIe VNAs, and the P937xA / P50xxA Streamline
USB VNAs.  These all speak the PNA SCPI dialect, which differs from the
E5071C (ENA) dialect in several load-bearing ways:

- Measurements are addressed by explicit number: ``CALC<ch>:MEAS<m>:...``.
  There is no selected-trace state, so a command cannot land on the wrong
  trace — the property the E5071C driver has to work around with
  ``CALC:PAR:SEL``.
- Limit tables are written WITHOUT a leading segment count (the ENA
  requires one), and cleared with ``LIM:DATA:DEL`` (the ENA has no delete
  command and clears via a zero-segment write).
- Formatted data (``DATA:FDATA?``) returns ONE value per point for scalar
  formats (the ENA returns two).
- Marker searches support up to 16 independent user ranges per channel
  (the ENA has a single shared search domain per channel).
"""
from __future__ import annotations

from typing import List, Tuple

from .base import InstrumentError, ScpiInstrument, format_limit_payload
from .models import SweepConfig, TraceSpec, EquationSpec, LimitTable, MarkerSpec, MarkerFunc

# (type, f_start_hz, f_stop_hz, y_start, y_stop)
LimitSeg = Tuple[int, float, float, float, float]


class PNA(ScpiInstrument):
    """Driver for PNA-family / new-platform Keysight VNAs (P50xx, P93xx, M98xx, E5080x).

    All trace-level methods take an explicit measurement number ``meas``
    (default 1) instead of the E5071C driver's ``meas_name`` — deterministic
    by construction.
    """

    # basics
    def preset(self) -> None:
        self.write("SYST:PRES")

    # configuration
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
        """Delete all measurements on a channel."""
        self.write(f"CALC{ch}:PAR:DEL:ALL")

    def define_trace(self, trace: TraceSpec, ch: int = 1, window: int = 1, meas: int = 1) -> None:
        """Create measurement number ``meas`` and feed it to a display window.

        The window is turned on first (CALC:MEAS:DEF creates the measurement
        but does not display it, and FEED needs an existing window).

        Note:
            The PNA platform addresses measurements by number, so
            ``trace.name`` is not sent to the instrument — pick ``meas``
            numbers instead and keep your own name→number mapping if you
            want names.
        """
        self.write(f'CALC{ch}:MEAS{meas}:DEF "{trace.sparam}"')
        self.write(f"DISP:WIND{window} ON")
        self.write(f"DISP:MEAS{meas}:FEED {window}")
        self.write(f"CALC{ch}:MEAS{meas}:FORM {trace.fmt}")

    def define_equation(self, eq: EquationSpec, ch: int = 1, meas: int = 1, check_valid: bool = True) -> None:
        """Apply an equation to an EXISTING measurement.

        Unlike the E5071C (where an equation is a trace type of its own),
        the PNA equation editor attaches an expression to an existing
        measurement — create one with define_trace() first, then apply the
        equation to it.  ``eq.name`` is not sent to the instrument unless
        you include it in the expression itself ("name=expr").

        Args:
            eq: The equation; expression may be bare ("S11/S21") or named
                ("foo=S11/S21").
            ch: Channel number.
            meas: Measurement number.
            check_valid: If True, query EQU:VALid? after loading and raise
                if the instrument rejected the expression — an invalid
                equation is otherwise silently ignored at sweep time.

        Raises:
            InstrumentError: If check_valid and the equation is not valid.
        """
        self.write(f'CALC{ch}:MEAS{meas}:EQU:TEXT "{eq.expression}"')
        if check_valid:
            valid = self.query(f"CALC{ch}:MEAS{meas}:EQU:VAL?").strip()
            if valid not in ("1", "+1"):
                raise InstrumentError(
                    f"equation rejected by instrument: {eq.expression!r} "
                    f"(CALC{ch}:MEAS{meas}:EQU:VAL? returned {valid!r})"
                )
        self.write(f"CALC{ch}:MEAS{meas}:EQU:STAT ON")

    # limits
    def limits_clear(self, ch: int = 1, meas: int = 1) -> None:
        """Delete all limit segments of a measurement (LIM:DATA:DEL)."""
        self.write(f"CALC{ch}:MEAS{meas}:LIM:DATA:DEL")

    def apply_limit_table_bulk(
        self,
        table: LimitTable,
        ch: int = 1,
        meas: int = 1,
        clear_first: bool = True,
        enable_display: bool = True,
        enable_test: bool = True,
        check_errors: bool = True,
    ) -> None:
        """Bulk-load a limit table via :CALC<ch>:MEAS<meas>:LIM:DATA.

        The PNA family expects segments WITHOUT a leading count:
          :CALC1:MEAS1:LIM:DATA <type1>,<fstart1>,<fstop1>,<ystart1>,<ystop1>,<type2>,...

        Where type is 1=MAX, 2=MIN (0=OFF), frequencies in Hz, and Y units
        follow the measurement's display format.

        The measurement number makes this deterministic: no trace selection
        is involved, so tables cannot land on the wrong trace.

        Raises:
            ValueError: empty table, invalid types, non-finite or reversed
                frequencies (validated before anything is written).
            InstrumentError: the instrument reported an error after the load
                (only if check_errors=True).
        """
        # Validate + flatten (raises ValueError before anything is written)
        payload = format_limit_payload(table, include_count=False)

        if check_errors:
            # Start from a clean status/error state so *ESR? below reflects
            # only this load, not stale errors from earlier commands.
            self.write("*CLS")

        if clear_first:
            self.write(f"CALC{ch}:MEAS{meas}:LIM:DATA:DEL")

        self.write(f"CALC{ch}:MEAS{meas}:LIM:DATA {payload}")

        if check_errors:
            self.opc()  # wait for command completion
            self.check_esr("Limit table load failed")

        if enable_display:
            self.write(f"CALC{ch}:MEAS{meas}:LIM:DISP ON")
        if enable_test:
            self.write(f"CALC{ch}:MEAS{meas}:LIM:STAT ON")

    def limit_fail(self, ch: int = 1, meas: int = 1) -> bool:
        """Return True if the limit test FAILED for any point on a measurement.

        Wraps CALC:MEAS:LIM:FAIL? (1 = fail, 0 = pass).  Trigger a sweep
        after enabling limit testing before trusting this.
        """
        return self.query(f"CALC{ch}:MEAS{meas}:LIM:FAIL?").strip() in ("1", "+1")

    def query_limit_table(self, ch: int = 1, meas: int = 1) -> List[LimitSeg]:
        """Read back a measurement's limit table.

        Returns:
            List of (type, f_start_hz, f_stop_hz, y_start, y_stop) tuples.
            The PNA returns segments with no leading count — just groups of
            five values.  Empty list if no limits are defined.
        """
        self.write("FORM:DATA ASC")
        values = self.query_values(f"CALC{ch}:MEAS{meas}:LIM:DATA?")
        if not values:
            return []
        segments: List[LimitSeg] = []
        for idx in range(0, len(values) - 4, 5):
            segments.append(
                (
                    int(values[idx]),
                    float(values[idx + 1]),
                    float(values[idx + 2]),
                    float(values[idx + 3]),
                    float(values[idx + 4]),
                )
            )
        return segments

    # sweep & data
    def single_sweep(self, ch: int = 1) -> None:
        """Run one sweep on a channel and block until it completes.

        PNA pattern: trigger source internal (required for SWE:MODE SINGle),
        then single-sweep mode, which *OPC? waits on.
        """
        self.write("TRIG:SOUR IMM")
        self.write(f"SENS{ch}:SWE:MODE SING")
        self.opc()

    def fetch_fdata(self, ch: int = 1, meas: int = 1) -> List[float]:
        """Read a measurement's formatted data.

        One value per point for scalar formats (MLOG, PHAS, SWR, ...);
        Polar and Smith formats return two values per point — unlike the
        E5071C, which always returns pairs.
        """
        self.write("FORM:DATA ASC")
        return self.query_values(f"CALC{ch}:MEAS{meas}:DATA:FDATA?")

    def fetch_frequencies(self, ch: int = 1, meas: int = 1) -> List[float]:
        """Read the stimulus (frequency) axis of a measurement, in Hz."""
        self.write("FORM:DATA ASC")
        return self.query_values(f"CALC{ch}:MEAS{meas}:X:VAL?")

    # markers
    def marker_enable(self, idx: int, ch: int = 1, meas: int = 1, on: bool = True) -> None:
        """Enable or disable a marker on a measurement.

        Args:
            idx: Marker number (1-15; marker 16 is the reference marker).
            ch: Channel number.
            meas: Measurement number.
            on: True to enable, False to disable.
        """
        self.write(f"CALC{ch}:MEAS{meas}:MARK{idx}:STAT {'ON' if on else 'OFF'}")

    def marker_set_range(
        self,
        idx: int,
        f_start_hz: float,
        f_stop_hz: float,
        ch: int = 1,
        meas: int = 1,
        user_range: int | None = None,
    ) -> None:
        """Assign a marker to a user search range and set that range's span.

        The PNA family has 16 user ranges per channel (range 0 = full span),
        so unlike the E5071C, different markers can search different bands
        simultaneously.  By default the marker uses the user range with its
        own number, which keeps ranges independent per marker; pass
        user_range explicitly to share a band between markers.

        Raises:
            ValueError: If f_stop_hz <= f_start_hz or user_range not in 1-16.
        """
        if f_stop_hz <= f_start_hz:
            raise ValueError(
                f"f_stop_hz must be > f_start_hz (got {f_start_hz} .. {f_stop_hz})"
            )
        rng = idx if user_range is None else user_range
        if not 1 <= rng <= 16:
            raise ValueError(f"user_range must be 1..16, got {rng}")
        base = f"CALC{ch}:MEAS{meas}:MARK{idx}:FUNC:DOM:USER"
        self.write(f"{base} {rng}")
        self.write(f"{base}:STAR {f_start_hz:.12g}")
        self.write(f"{base}:STOP {f_stop_hz:.12g}")

    def marker_search(self, idx: int, func: MarkerFunc, ch: int = 1, meas: int = 1) -> None:
        """Execute a marker search (MIN or MAX) within the marker's range."""
        self.write(f"CALC{ch}:MEAS{meas}:MARK{idx}:FUNC:EXEC {func.value}")

    def marker_configure(self, spec: MarkerSpec, ch: int = 1, meas: int = 1) -> None:
        """Configure a marker from a MarkerSpec.

        Orchestrates: enable -> assign user range (if enabled) -> execute
        search.  Does NOT call opc(); sync once after configuring all markers.

        Note:
            spec.index doubles as the user-range number, so each marker
            searches its own band — no shared-domain conflicts like the
            E5071C.
        """
        self.marker_enable(spec.index, ch=ch, meas=meas, on=True)
        if spec.enable_domain:
            self.marker_set_range(spec.index, spec.f_start_hz, spec.f_stop_hz, ch=ch, meas=meas)
        self.marker_search(spec.index, spec.func, ch=ch, meas=meas)

    def marker_read(self, idx: int, ch: int = 1, meas: int = 1) -> Tuple[float, float]:
        """Read a marker's X (stimulus) and Y (primary response) values."""
        x = float(self.query(f"CALC{ch}:MEAS{meas}:MARK{idx}:X?").strip())
        parts = self.query(f"CALC{ch}:MEAS{meas}:MARK{idx}:Y?").strip().split(",")
        y = float(parts[0])
        return (x, y)
