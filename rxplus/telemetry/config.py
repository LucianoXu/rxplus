"""OTel provider configuration for rxplus components.

Provides :func:`configure_telemetry` (tracer + logger providers),
:func:`configure_metrics` (meter provider), and :func:`get_default_providers`
(lazy singleton with console output).
"""

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from .exporters import ConsoleLogRecordExporter


def configure_telemetry(
    service_name: str = "rxplus",
    service_version: str = "",
    span_exporter: SpanExporter | None = None,
    log_exporter: LogRecordExporter | None = None,
    batch_logs: bool = True,
) -> tuple[TracerProvider, LoggerProvider]:
    """
    Configure OTel providers for rxplus components.

    Creates TracerProvider and LoggerProvider with the specified resource
    attributes and exporters. Returns the providers for explicit injection
    into components -- does NOT set global providers.

    Args:
        service_name: Service identifier for resource attributes.
        service_version: Service version for resource attributes.
        span_exporter: Optional span exporter
            (e.g., OTLPSpanExporter, ConsoleSpanExporter).
        log_exporter: Optional log exporter
            (e.g., OTLPLogExporter, ConsoleLogExporter).
        batch_logs: If True, use BatchLogRecordProcessor
            (better for network exporters). If False, use
            SimpleLogRecordProcessor (immediate, better for console).

    Returns:
        Tuple of (TracerProvider, LoggerProvider) for injection into components.

    Example:
        >>> from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        >>> from opentelemetry.sdk._logs.export import ConsoleLogExporter
        >>>
        >>> tracer_provider, logger_provider = configure_telemetry(
        ...     service_name="my-app",
        ...     span_exporter=ConsoleSpanExporter(),
        ...     log_exporter=ConsoleLogExporter(),
        ... )
        >>>
        >>> server = RxWSServer(
        ...     {"host": "::", "port": 8888},
        ...     tracer_provider=tracer_provider,
        ...     logger_provider=logger_provider,
        ... )
    """
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    if span_exporter:
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))

    logger_provider = LoggerProvider(resource=resource)
    if log_exporter:
        if batch_logs:
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(log_exporter)
            )
        else:
            logger_provider.add_log_record_processor(
                SimpleLogRecordProcessor(log_exporter)
            )

    return tracer_provider, logger_provider


# =============================================================================
# Default Providers
# =============================================================================


_default_tracer_provider: TracerProvider | None = None
_default_logger_provider: LoggerProvider | None = None


def get_default_providers(
    service_name: str = "rxplus",
) -> tuple[TracerProvider, LoggerProvider]:
    """Get or create default providers with console output.

    Lazily initializes default providers on first call. Returns the same
    providers on subsequent calls (singleton pattern).

    The default configuration uses ConsoleLogRecordExporter for CLI-friendly
    output to stderr with immediate (non-batched) processing.
    Users can override by passing custom providers to component constructors.

    Args:
        service_name: Service name for the default providers (only used on first call).

    Returns:
        Tuple of (TracerProvider, LoggerProvider) for injection into components.

    Example:
        >>> tracer_provider, logger_provider = get_default_providers()
        >>> server = RxWSServer(config, logger_provider=logger_provider)
    """
    global _default_tracer_provider, _default_logger_provider

    if _default_logger_provider is None:
        _default_tracer_provider, _default_logger_provider = configure_telemetry(
            service_name=service_name,
            log_exporter=ConsoleLogRecordExporter(),
            batch_logs=False,  # Immediate output for CLI
        )

    # At this point both providers are guaranteed to be initialized
    assert _default_tracer_provider is not None
    return _default_tracer_provider, _default_logger_provider


# =============================================================================
# Metrics Configuration
# =============================================================================


def configure_metrics(
    service_name: str = "rxplus",
    service_version: str = "",
    metric_exporter: MetricExporter | None = None,
    export_interval_ms: int = 10_000,
) -> MeterProvider:
    """Configure and return an OTel MeterProvider.

    This function is separate from ``configure_telemetry`` to preserve
    backward compatibility with existing callers that unpack a two-tuple
    ``(TracerProvider, LoggerProvider)``.

    Args:
        service_name: Service identifier added to all metrics as a resource
            attribute.
        service_version: Service version resource attribute.
        metric_exporter: Optional metric exporter (e.g.
            ``OTLPMetricExporter``).  If ``None``, metrics are exported to
            ``ConsoleMetricExporter`` at the configured interval.
        export_interval_ms: Polling interval for
            ``PeriodicExportingMetricReader`` (milliseconds).  Default 10 s.

    Returns:
        Configured :class:`MeterProvider`.

    Example::

        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )

        meter_provider = configure_metrics(
            service_name="dhproto.cpmo-backend",
            metric_exporter=OTLPMetricExporter(
                endpoint="http://otel-collector.example.com:13858",
                insecure=True,
            ),
        )
        helper = MetricsHelper(meter_provider, "dhproto.cpmo_backend")
        counter = helper.counter("cpmo.audio.chunks.inbound")
    """
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
        }
    )

    exporter = (
        metric_exporter if metric_exporter is not None else ConsoleMetricExporter()
    )
    reader = PeriodicExportingMetricReader(
        exporter, export_interval_millis=export_interval_ms
    )
    return MeterProvider(resource=resource, metric_readers=[reader])
