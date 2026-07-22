import pytest

from vna_library import LimitSegment, LimitTable, LimitType, MarkerSpec, SweepConfig


def test_sweep_config_defaults():
    cfg = SweepConfig(start_hz=1e9, stop_hz=2e9)
    assert cfg.points == 601
    assert cfg.avg_count == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(start_hz=2e9, stop_hz=1e9),          # reversed band
        dict(start_hz=1e9, stop_hz=1e9),          # zero-width band
        dict(start_hz=1e9, stop_hz=2e9, points=1),
        dict(start_hz=1e9, stop_hz=2e9, if_bw_hz=0),
        dict(start_hz=1e9, stop_hz=2e9, avg_count=0),
        dict(start_hz=float("nan"), stop_hz=2e9),
    ],
)
def test_sweep_config_rejects_invalid(kwargs):
    with pytest.raises(ValueError):
        SweepConfig(**kwargs)


def test_limit_segment_valid():
    seg = LimitSegment(LimitType.MAX, 1e9, 2e9, -10.0, -10.0)
    assert seg.kind is LimitType.MAX


@pytest.mark.parametrize(
    "args",
    [
        (LimitType.MAX, 2e9, 1e9, 0.0, 0.0),          # reversed band
        (LimitType.MIN, 1e9, 2e9, float("inf"), 0.0),  # non-finite y
        (3, 1e9, 2e9, 0.0, 0.0),                       # bad kind
    ],
)
def test_limit_segment_rejects_invalid(args):
    with pytest.raises(ValueError):
        LimitSegment(*args)


def test_limit_table_from_iter():
    segs = [
        LimitSegment(LimitType.MAX, 1e9, 2e9, -10.0, -10.0),
        LimitSegment(LimitType.MIN, 1e9, 2e9, -60.0, -60.0),
    ]
    table = LimitTable.from_iter(segs)
    assert table.segments == tuple(segs)


@pytest.mark.parametrize("index", [0, 11])
def test_marker_spec_rejects_bad_index(index):
    with pytest.raises(ValueError):
        MarkerSpec(index=index, f_start_hz=1e9, f_stop_hz=2e9)


def test_marker_spec_rejects_reversed_band():
    with pytest.raises(ValueError):
        MarkerSpec(index=1, f_start_hz=2e9, f_stop_hz=1e9)


def test_marker_spec_reversed_band_ok_when_domain_disabled():
    spec = MarkerSpec(index=1, f_start_hz=0.0, f_stop_hz=0.0, enable_domain=False)
    assert spec.enable_domain is False
