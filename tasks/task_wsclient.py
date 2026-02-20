import argparse
import time

from opentelemetry._logs import LogRecord as OTelLogRecord
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from rxplus import (
    RxWSClient,
    configure_telemetry,
)


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("wsclient", help="start the ws client.")
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--path", type=str, default="/")
    parser.add_argument(
        "--otlp-endpoint",
        type=str,
        default="http://localhost:4318",
        help="OTLP HTTP endpoint for telemetry export (OTel Collector)",
    )
    parser.set_defaults(func=task)


def task(parsed_args: argparse.Namespace):
    """
    Demonstrate WebSocket client with OTel telemetry.

    This client:
    1. Connects to a WebSocket server
    2. Creates spans for operations
    3. Emits logs via OTel
    4. Sends/receives data over WebSocket
    """
    # Configure OTel with OTLP exporters
    log_exporter = OTLPLogExporter(endpoint=f"{parsed_args.otlp_endpoint}/v1/logs")
    span_exporter = OTLPSpanExporter(endpoint=f"{parsed_args.otlp_endpoint}/v1/traces")

    tracer_provider, logger_provider = configure_telemetry(
        log_exporter=log_exporter,
        span_exporter=span_exporter,
    )

    # Create client with telemetry providers
    receiver = RxWSClient(
        {
            "host": parsed_args.host,
            "port": parsed_args.port,
            "path": parsed_args.path,
        },
        datatype="string",
        buffer_while_disconnected=True,
        tracer_provider=tracer_provider,
        logger_provider=logger_provider,
    )

    # Subscribe to print received data
    receiver.subscribe(
        on_next=lambda x: print(f"[RECEIVED] {x}"),
        on_error=lambda e: print(f"Error: {e}"),
        on_completed=lambda: print("Client completed"),
    )

    # Get tracer and logger for this module
    tracer = tracer_provider.get_tracer("task_wsclient")
    logger = logger_provider.get_logger("task_wsclient")

    print(
        f"Client connecting to {parsed_args.host}:{parsed_args.port}{parsed_args.path}"
    )
    print(f"Telemetry exported via OTLP to: {parsed_args.otlp_endpoint}")
    print("Will send messages every 2 seconds...")
    print("-" * 60)

    i = 0
    try:
        while True:
            time.sleep(2.0)

            # Create a span for this operation
            with tracer.start_as_current_span("send_ping") as span:
                span.set_attribute("ping_id", i)

                # Emit a log record
                log_record = OTelLogRecord(
                    timestamp=time.time_ns(),
                    body=f"Client ping #{i}",
                    severity_text="DEBUG",
                    severity_number=SeverityNumber.DEBUG,
                    attributes={"ping_id": i},
                )
                logger.emit(log_record)

                print(f"[SENDING] Ping #{i}")

                # Send data over WebSocket
                receiver.on_next(f"Client ping #{i}")

            i += 1

    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
        receiver.on_completed()
        tracer_provider.shutdown()
        logger_provider.shutdown()
