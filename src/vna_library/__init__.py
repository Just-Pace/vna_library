# src/vna_library/__init__.py
from .base import VNA, ScpiVNA, VNAError, InstrumentError, VNATimeout
from .e5071c import E5071C
from .pna_family import PNAFamily
from .factory import connect, driver_for_idn
from .models import (
    SweepConfig, TraceSpec, EquationSpec,
    LimitType, LimitSegment, LimitTable,
    MarkerFunc, MarkerSpec,
)

__all__ = [
    # interface & factory
    "VNA", "ScpiVNA", "connect", "driver_for_idn",
    # drivers
    "E5071C", "PNAFamily",
    # errors
    "VNAError", "InstrumentError", "VNATimeout",
    # models
    "SweepConfig", "TraceSpec", "EquationSpec",
    "LimitType", "LimitSegment", "LimitTable",
    "MarkerFunc", "MarkerSpec",
]
