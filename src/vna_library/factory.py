# src/vna_library/factory.py
"""
Instrument autodetection: open a VISA resource, read *IDN?, and return
the matching driver so recipes never hard-code an instrument class.

    from vna_library import connect
    vna = connect("TCPIP0::192.168.0.10::inst0::INSTR")   # -> E5071C or PNAFamily
"""
from __future__ import annotations

from typing import Optional, Type

from .base import ScpiVNA, VNA, VNAError
from .e5071c import E5071C
from .pna_family import PNAFamily

__all__ = ["connect", "driver_for_idn"]

# Model-number prefixes (second *IDN? field) -> driver class.  Checked in
# order; first match wins.
_MODEL_PREFIXES: list[tuple[str, Type[ScpiVNA]]] = [
    ("E5071", E5071C),      # classic ENA
    ("E5061", E5071C),      # ENA-L shares the classic ENA command set
    ("E5062", E5071C),
    ("E5080", PNAFamily),   # modern ENA
    ("E5081", PNAFamily),   # ENA-X
    ("N52", PNAFamily),     # PNA / PNA-L / PNA-X (N522x/N523x/N524x/N5251...)
    ("P50", PNAFamily),     # Streamline USB VNAs
    ("M980", PNAFamily),    # PXIe VNAs
]


def driver_for_idn(idn: str) -> Type[ScpiVNA]:
    """Pick a driver class from a raw *IDN? response.

    Raises:
        VNAError: If no driver matches the model field.
    """
    fields = [f.strip() for f in idn.split(",")]
    model = fields[1].upper() if len(fields) >= 2 else idn.strip().upper()
    for prefix, cls in _MODEL_PREFIXES:
        if model.startswith(prefix):
            return cls
    raise VNAError(
        f"No driver for instrument {idn!r}. Known families: "
        f"{', '.join(p for p, _ in _MODEL_PREFIXES)}. "
        f"Instantiate a driver class directly if the command set matches."
    )


def connect(resource: str, timeout_ms: int = 30000,
            driver: Optional[Type[ScpiVNA]] = None) -> VNA:
    """Open `resource`, autodetect the instrument, and return its driver.

    Args:
        resource: VISA resource string
            (e.g. "TCPIP0::192.168.0.10::inst0::INSTR").
        timeout_ms: VISA I/O timeout.
        driver: Skip autodetection and force this driver class.

    Returns:
        A connected driver implementing the `VNA` interface.

    Raises:
        VNAError: If *IDN? matches no known driver.
    """
    import pyvisa

    rm = pyvisa.ResourceManager()
    inst = rm.open_resource(resource)
    inst.timeout = timeout_ms
    inst.write_termination = "\n"
    inst.read_termination = "\n"
    if driver is None:
        idn = inst.query("*IDN?").strip()
        try:
            driver = driver_for_idn(idn)
        except VNAError:
            inst.close()
            raise
    return driver(inst)
