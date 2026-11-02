"""Tests for VISA backend selection and fallback in base._open_visa."""
from types import SimpleNamespace

import pytest

import vna_library.base as base
from vna_library import E5071C, InstrumentError


def make_pyvisa_stub(behavior):
    """behavior(spec) -> resource-manager-like object, or raises."""

    class ResourceManager:
        def __init__(self, spec=None):
            self.rm = behavior(spec)

        def open_resource(self, resource):
            return SimpleNamespace(resource=resource, timeout=None,
                                   write_termination=None, read_termination=None)

    return SimpleNamespace(
        ResourceManager=ResourceManager,
        errors=SimpleNamespace(VisaIOError=type("VisaIOError", (Exception,), {})),
        constants=SimpleNamespace(StatusCode=SimpleNamespace(error_timeout=object())),
    )


def test_falls_back_to_pyvisa_py_when_no_vendor_backend(monkeypatch):
    calls = []

    def behavior(spec):
        calls.append(spec)
        if spec is None:
            raise ValueError("Could not locate a VISA implementation")
        assert spec == "@py"
        return object()

    monkeypatch.setattr(base, "pyvisa", make_pyvisa_stub(behavior))
    vna = E5071C.open("TCPIP0::1.2.3.4::hislip0::INSTR")
    assert calls == [None, "@py"]
    assert vna.inst.resource == "TCPIP0::1.2.3.4::hislip0::INSTR"
    assert vna.inst.write_termination == "\n"


def test_actionable_error_when_no_backend_at_all(monkeypatch):
    def behavior(spec):
        raise ValueError("Could not locate a VISA implementation")

    monkeypatch.setattr(base, "pyvisa", make_pyvisa_stub(behavior))
    with pytest.raises(InstrumentError, match="pip install pyvisa-py"):
        E5071C.open("TCPIP0::1.2.3.4::hislip0::INSTR")


def test_explicit_backend_is_used_verbatim(monkeypatch):
    calls = []

    def behavior(spec):
        calls.append(spec)
        return object()

    monkeypatch.setattr(base, "pyvisa", make_pyvisa_stub(behavior))
    E5071C.open("GPIB0::16::INSTR", backend="@py")
    assert calls == ["@py"]


def test_missing_pyvisa_gives_clear_importerror(monkeypatch):
    monkeypatch.setattr(base, "pyvisa", None)
    with pytest.raises(ImportError, match="pyvisa is required"):
        E5071C.open("GPIB0::16::INSTR")
