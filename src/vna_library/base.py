# src/vna_library/base.py
"""Shared transport layer and SCPI plumbing for all VNA drivers."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, List, Protocol

if TYPE_CHECKING:
    from .models import LimitTable

try:
    import pyvisa
except ImportError:  # pragma: no cover - pyvisa only needed for real hardware
    pyvisa = None


class VNAError(Exception): ...
class InstrumentError(VNAError): ...
class VNATimeout(VNAError): ...


class VisaSession(Protocol):
    """The minimal transport interface a driver needs.

    Satisfied by a pyvisa MessageBasedResource, or by a fake in tests.
    """
    timeout: Any

    def write(self, cmd: str) -> Any: ...
    def query(self, cmd: str) -> str: ...
    def query_ascii_values(self, cmd: str) -> List[float]: ...
    def close(self) -> None: ...


def _open_visa(resource: str, timeout_ms: int) -> VisaSession:
    if pyvisa is None:
        raise ImportError(
            "pyvisa is required to open a real instrument; "
            "install it or pass a session object instead of a resource string"
        )
    rm = pyvisa.ResourceManager()
    session = rm.open_resource(resource)
    session.timeout = timeout_ms
    session.write_termination = "\n"
    session.read_termination = "\n"
    return session


def format_limit_payload(table: "LimitTable", include_count: bool) -> str:
    """Validate a LimitTable and flatten it into a SCPI comma stream.

    Segments are emitted as 5-value groups (type, f_start, f_stop, y_start,
    y_stop).  The ENA (E5071C) expects a leading segment count; the PNA family
    does not — hence include_count.

    Raises:
        ValueError: empty table, invalid type, non-finite numbers, or
            f_stop_hz <= f_start_hz.
    """
    segments = [
        (int(seg.kind), seg.f_start_hz, seg.f_stop_hz, seg.y_start_db, seg.y_stop_db)
        for seg in table.segments
    ]
    if not segments:
        raise ValueError("segments is empty")

    flat: List[str] = [str(len(segments))] if include_count else []
    for i, (typ, fs, fe, ys, ye) in enumerate(segments, start=1):
        if typ not in (1, 2):
            raise ValueError(f"segment {i}: typ must be 1 (MAX) or 2 (MIN), got {typ}")
        for name, v in (("fs", fs), ("fe", fe), ("ys", ys), ("ye", ye)):
            if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise ValueError(f"segment {i}: {name} must be a finite number, got {v!r}")
        if fe <= fs:
            raise ValueError(f"segment {i}: f_stop_hz must be > f_start_hz (got {fs} .. {fe})")
        # Keep formatting "boring" (no thousands separators, no units).
        # .12g is usually safe for SCPI numeric parsing.
        flat.append(str(int(typ)))
        flat.append(f"{float(fs):.12g}")
        flat.append(f"{float(fe):.12g}")
        flat.append(f"{float(ys):.12g}")
        flat.append(f"{float(ye):.12g}")
    return ",".join(flat)


class ScpiInstrument:
    """Base class: session handling, error translation, IEEE-488.2 basics."""

    def __init__(self, session: VisaSession | str, timeout_ms: int = 30000):
        if isinstance(session, str):
            session = _open_visa(session, timeout_ms)
        self.inst = session

    @classmethod
    def open(cls, resource: str, timeout_ms: int = 30000):
        """Open a real instrument by VISA resource string."""
        return cls(_open_visa(resource, timeout_ms))

    def close(self) -> None:
        self.inst.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    # low-level
    def _translate(self, exc: Exception, cmd: str) -> Exception:
        if pyvisa is not None and isinstance(exc, pyvisa.errors.VisaIOError):
            if exc.error_code == pyvisa.constants.StatusCode.error_timeout:
                return VNATimeout(f"timeout during {cmd!r}")
            return InstrumentError(f"VISA error during {cmd!r}: {exc}")
        return exc

    def write(self, cmd: str) -> None:
        try:
            self.inst.write(cmd)
        except Exception as exc:
            raise self._translate(exc, cmd) from exc

    def query(self, cmd: str) -> str:
        try:
            return self.inst.query(cmd)
        except Exception as exc:
            raise self._translate(exc, cmd) from exc

    def query_values(self, cmd: str) -> List[float]:
        try:
            return self.inst.query_ascii_values(cmd)
        except Exception as exc:
            raise self._translate(exc, cmd) from exc

    def opc(self) -> None:
        self.query("*OPC?")

    def get_errors(self) -> List[str]:
        """
        Query and return all errors from the instrument error queue.

        Returns:
            List of error strings. Empty list if no errors.
        """
        errors = []
        while True:
            err = self.query("SYST:ERR?").strip()
            try:
                code = int(err.split(",", 1)[0])
            except ValueError:
                code = None
            if code == 0:
                break
            errors.append(err)
            if code is None:
                break
        return errors

    def check_esr(self, context: str) -> None:
        """Raise InstrumentError if the event status register shows an error.

        Checks bits 2-5 (query, device-dependent, execution, command error).
        Call *CLS before the operation under test so stale bits don't trip this.
        """
        esr = int(self.query("*ESR?"))
        if esr & 0b00111100:
            errors = self.get_errors()
            raise InstrumentError(
                f"{context} – instrument error: {errors[0] if errors else 'Unknown error'}"
            )

    # basics
    def idn(self) -> str:
        return self.query("*IDN?").strip()

    def reset(self) -> None:
        self.write("*RST")
