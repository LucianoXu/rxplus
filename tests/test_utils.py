from rxplus.utils import TaggedData, get_full_error_info, get_short_error_info


def test_tagged_data_repr_str():
    td = TaggedData("tag", 123)
    assert str(td) == "(tag=tag, 123)"
    assert repr(td) == "(tag=tag, 123)"


def test_error_info_functions():
    try:
        raise ValueError("oops")
    except Exception as e:
        short = get_short_error_info(e)
        full = get_full_error_info(e)

    assert "ValueError" in short and "oops" in short
    assert "ValueError" in full and "oops" in full


def test_monitors_report_through_a_callback(capsys):
    """0.2: no print; the default report is an OTel log."""
    from rxplus import BandwidthMonitor, FPSMonitor

    fps: list[float] = []
    bw: list[float] = []
    fpsm = FPSMonitor(interval=0.0, report=fps.append)
    bwm = BandwidthMonitor(interval=0.0, scale=0.5, report=bw.append)
    assert fpsm("x") == "x" and bwm(b"abcd") == b"abcd"
    assert len(fps) == 1 and fps[0] > 0
    assert len(bw) == 1 and bw[0] > 0
    FPSMonitor(interval=0.0)("x")  # the default report: an OTel log, no print
    BandwidthMonitor(interval=0.0)(b"x")
    assert capsys.readouterr().out == ""


def test_the_default_monitor_report_goes_to_the_given_logger_provider():
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        InMemoryLogRecordExporter,
        SimpleLogRecordProcessor,
    )

    from rxplus import FPSMonitor

    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    FPSMonitor(interval=0.0, logger_provider=provider)("x")
    bodies = [str(r.log_record.body) for r in exporter.get_finished_logs()]
    assert any(b.startswith("[FPS]") for b in bodies), bodies


def test_bandwidth_monitor_counts_items_without_a_usable_len_as_zero():
    """Review of #31: a 0-d numpy array has __len__ but raises TypeError."""
    import numpy as np

    from rxplus import BandwidthMonitor

    seen: list[float] = []
    bm = BandwidthMonitor(interval=0.0, report=seen.append)
    arr = np.array(1.0)
    assert bm(arr) is arr and bm(3) == 3
    assert seen == [0.0, 0.0]


def test_with_context_source_none_keeps_the_source():
    from opentelemetry.sdk._logs import LoggerProvider

    from rxplus import OTelLogger

    log = OTelLogger(LoggerProvider().get_logger("x"), source="Parent")
    assert log.with_context(source=None)._source == "Parent"
    assert log.with_context(source="Child")._source == "Child"
