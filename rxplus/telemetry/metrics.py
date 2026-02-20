"""OTel metrics helper for reactive components.

Provides :class:`MetricsHelper` — a convenience wrapper around an OTel
``Meter`` for creating counters and histograms.
"""

from opentelemetry.metrics import Counter, Histogram, Meter
from opentelemetry.sdk.metrics import MeterProvider


class MetricsHelper:
    """Convenience wrapper around an OTel ``Meter``.

    Simplifies creation of counters and histograms for instrumentation inside
    reactive components.

    Args:
        meter_provider: The :class:`MeterProvider` to obtain a meter from.
        instrumentation_name: Identifies the instrumentation library (usually
            the module or class name, e.g. ``"dhproto.cpmo_backend"``).

    Example::

        helper = MetricsHelper(meter_provider, "dhproto.cpmo_backend")
        chunks_in = helper.counter(
            "cpmo.audio.chunks.inbound",
            description="Audio chunks received from client",
        )
        size_hist = helper.histogram(
            "cpmo.audio.chunk_bytes.inbound",
            description="Audio chunk sizes (bytes)",
            unit="By",
        )
        # later:
        chunks_in.add(1, {"namespace": ns})
        size_hist.record(len(chunk), {"namespace": ns})
    """

    def __init__(self, meter_provider: MeterProvider, instrumentation_name: str):
        self._meter: Meter = meter_provider.get_meter(instrumentation_name)

    def counter(
        self,
        name: str,
        description: str = "",
        unit: str = "1",
    ) -> Counter:
        """Create (or retrieve) a monotonic counter instrument.

        Args:
            name: Metric name (e.g. ``"cpmo.audio.chunks.inbound"``).
            description: Human-readable description.
            unit: UCUM unit string (default ``"1"`` = dimensionless).

        Returns:
            OTel :class:`Counter` instrument.
        """
        return self._meter.create_counter(name, description=description, unit=unit)

    def histogram(
        self,
        name: str,
        description: str = "",
        unit: str = "ms",
    ) -> Histogram:
        """Create (or retrieve) a histogram instrument.

        Args:
            name: Metric name (e.g. ``"cpmo.audio.chunk_bytes.inbound"``).
            description: Human-readable description.
            unit: UCUM unit string (default ``"ms"`` = milliseconds).

        Returns:
            OTel :class:`Histogram` instrument.
        """
        return self._meter.create_histogram(name, description=description, unit=unit)
