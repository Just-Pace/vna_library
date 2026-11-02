"""Behavior-level tests: the PNA driver against a stateful PNA simulator.

Regressions covered from the 2026-11 review:
- [P1] clear_traces must be channel-scoped (CALC:PAR:DEL:ALL is instrument-wide).
- [P1] marker user ranges are channel-global; same-index markers on different
  measurements must not share (and thereby move) each other's search bounds.
- [P2] enable_domain=False must return the marker to full span, not silently
  keep a previously configured restriction.
"""
import pytest

from vna_library import (
    PNA,
    EquationSpec,
    InstrumentError,
    LimitSegment,
    LimitTable,
    LimitType,
    MarkerFunc,
    MarkerSpec,
    TraceSpec,
)
from tests.simulators import SimulatedPNA


@pytest.fixture
def sim():
    return SimulatedPNA(f_start=1.0e9, f_stop=2.0e9, points=11)


@pytest.fixture
def pna(sim):
    return PNA(sim)


def table(lo, hi, kind=LimitType.MAX, y=-10.0):
    return LimitTable.from_iter([LimitSegment(kind, lo, hi, y, y)])


def test_clear_traces_is_channel_scoped(pna, sim):
    """[P1] Clearing channel 1 must not delete channel 2's measurements."""
    pna.define_trace(TraceSpec(name="a", sparam="S11"), ch=1, meas=1)
    pna.define_trace(TraceSpec(name="b", sparam="S21"), ch=1, meas=2)
    pna.define_trace(TraceSpec(name="c", sparam="S11"), ch=2, meas=3)
    pna.clear_traces(ch=1)
    assert set(sim.meas) == {3}
    assert not sim.error_queue


def test_clear_traces_on_empty_channel_is_a_noop(pna, sim):
    pna.define_trace(TraceSpec(name="c", sparam="S11"), ch=2, meas=3)
    pna.clear_traces(ch=1)
    assert set(sim.meas) == {3}


def test_delete_all_measurements_is_explicitly_instrument_wide(pna, sim):
    pna.define_trace(TraceSpec(name="a", sparam="S11"), ch=1, meas=1)
    pna.define_trace(TraceSpec(name="c", sparam="S11"), ch=2, meas=3)
    pna.delete_all_measurements()
    assert sim.meas == {}


def test_marker_ranges_do_not_collide_across_measurements(pna, sim):
    """[P1] Marker 1 on meas 1 and marker 1 on meas 2 get distinct ranges, so
    configuring the second leaves the first marker's search bounds intact."""
    pna.define_trace(TraceSpec(name="a", sparam="S11"), meas=1)
    pna.define_trace(TraceSpec(name="b", sparam="S21"), meas=2)
    spec1 = MarkerSpec(index=1, f_start_hz=1.2e9, f_stop_hz=1.3e9, func=MarkerFunc.MIN)
    spec2 = MarkerSpec(index=1, f_start_hz=1.7e9, f_stop_hz=1.8e9, func=MarkerFunc.MIN)
    pna.marker_configure(spec1, meas=1)
    pna.marker_configure(spec2, meas=2)
    # Re-run meas 1's search AFTER meas 2 was configured:
    pna.marker_search(1, MarkerFunc.MIN, meas=1)
    x1, _ = pna.marker_read(1, meas=1)
    x2, _ = pna.marker_read(1, meas=2)
    assert x1 == 1.2e9  # unchanged — would be 1.7e9 with the old shared default
    assert x2 == 1.7e9


def test_explicit_range_collision_is_refused(pna):
    pna.marker_set_range(1, 1.2e9, 1.3e9, meas=1, user_range=5)
    with pytest.raises(InstrumentError, match="already used"):
        pna.marker_set_range(1, 1.7e9, 1.8e9, meas=2, user_range=5)
    # Same (meas, marker) re-using its own range is fine:
    pna.marker_set_range(1, 1.25e9, 1.35e9, meas=1, user_range=5)


def test_ranges_exhaust_at_sixteen(pna):
    for i in range(16):
        pna.marker_set_range(1 + i % 15, 1.1e9, 1.2e9, meas=1 + i // 15)
    with pytest.raises(InstrumentError, match="16 user ranges"):
        pna.marker_set_range(15, 1.1e9, 1.2e9, meas=3)


def test_disabling_domain_restores_full_span(pna, sim):
    """[P2] Reconfiguring with enable_domain=False must not keep the old range."""
    pna.define_trace(TraceSpec(name="a", sparam="S11"), meas=1)
    ranged = MarkerSpec(index=1, f_start_hz=1.2e9, f_stop_hz=1.8e9, func=MarkerFunc.MIN)
    pna.marker_configure(ranged, meas=1)
    x, _ = pna.marker_read(1, meas=1)
    assert x == 1.2e9
    full = MarkerSpec(index=1, f_start_hz=0.0, f_stop_hz=0.0, func=MarkerFunc.MIN, enable_domain=False)
    pna.marker_configure(full, meas=1)
    x, _ = pna.marker_read(1, meas=1)
    assert x == 1.0e9  # full span again


def test_limits_land_per_measurement_and_roundtrip(pna, sim):
    pna.define_trace(TraceSpec(name="a", sparam="S11"), meas=1)
    pna.define_trace(TraceSpec(name="b", sparam="S21"), meas=2)
    pna.apply_limit_table_bulk(table(1.1e9, 1.2e9, y=-10.0), meas=1)
    pna.apply_limit_table_bulk(table(1.5e9, 1.6e9, y=-60.0), meas=2)
    assert pna.query_limit_table(meas=1) == [(1, 1.1e9, 1.2e9, -10.0, -10.0)]
    assert pna.query_limit_table(meas=2) == [(1, 1.5e9, 1.6e9, -60.0, -60.0)]
    assert sim.limit_test[1] and sim.limit_test[2]
    assert pna.limit_fail(meas=1) is False


def test_limit_load_on_missing_measurement_raises(pna):
    with pytest.raises(InstrumentError):
        pna.apply_limit_table_bulk(table(1.1e9, 1.2e9), meas=9)


def test_equation_validity_checked_on_instrument(pna, sim):
    pna.define_trace(TraceSpec(name="a", sparam="S11"), meas=1)
    pna.define_equation(EquationSpec(name="am", expression="sqrt(abs(S11)*abs(S11))"), meas=1)
    assert sim.eq_state[1] is True
    with pytest.raises(InstrumentError, match="equation rejected"):
        pna.define_equation(EquationSpec(name="bad", expression="sqrt(S11"), meas=1)


def test_single_sweep_uses_internal_trigger(pna, sim):
    pna.single_sweep()
    assert sim.sweeps_run == 1
    assert not sim.error_queue


def test_sweep_mode_single_fails_on_bus_trigger(sim):
    sim.write("TRIG:SOUR BUS")
    sim.write("SENS1:SWE:MODE SING")
    assert sim.sweeps_run == 0
    assert any("-221" in e for e in sim.error_queue)


def test_data_fetches_match_point_count(pna, sim):
    pna.define_trace(TraceSpec(name="a", sparam="S11"), meas=1)
    assert len(pna.fetch_frequencies(meas=1)) == sim.points
    assert len(pna.fetch_fdata(meas=1)) == sim.points


def test_feed_requires_window_but_driver_creates_it(pna, sim):
    pna.define_trace(TraceSpec(name="a", sparam="S11"), meas=1, window=3)
    assert 3 in sim.windows
    assert not sim.error_queue
