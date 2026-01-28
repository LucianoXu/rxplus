import argparse
import time

from rxplus import (
    stream_print_out,
    RxWSServer,
    TaggedData,
    untag,
    configure_telemetry,
)

from opentelemetry._logs import SeverityNumber
from opentelemetry._logs import LogRecord as OTelLogRecord
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("wsserver", help="start the ws server.")
    parser.add_argument("--host", type=str, default="::")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument(
        "--otlp-endpoint",
        type=str,
        default="http://localhost:4318",
        help="OTLP HTTP endpoint for telemetry export (OTel Collector)",
    )
    parser.set_defaults(func=task)


def task(parsed_args: argparse.Namespace):
    """
    Demonstrate WebSocket server with OTel telemetry.
    
    This server:
    1. Creates spans for operations
    2. Emits logs via OTel
    3. Sends/receives data over WebSocket
    """
    # Configure OTel with OTLP exporters
    log_exporter = OTLPLogExporter(endpoint=f"{parsed_args.otlp_endpoint}/v1/logs")
    span_exporter = OTLPSpanExporter(endpoint=f"{parsed_args.otlp_endpoint}/v1/traces")
    
    tracer_provider, logger_provider = configure_telemetry(
        log_exporter=log_exporter,
        span_exporter=span_exporter,
    )
    
    # Create server with telemetry providers
    sender = RxWSServer(
        {
            'host': parsed_args.host,
            'port': parsed_args.port,
        },
        datatype='string',
        tracer_provider=tracer_provider,
        logger_provider=logger_provider,
    )

    # Subscribe to print received data
    sender.pipe(
        stream_print_out(prompt="[RECEIVED] "),
        untag()
    ).subscribe(
        on_next=lambda x: None,  # Already printed
        on_error=lambda e: print(f"Error: {e}"),
        on_completed=lambda: print("Server completed"),
    )

    # Get tracer and logger for this module
    tracer = tracer_provider.get_tracer("task_wsserver")
    logger = logger_provider.get_logger("task_wsserver")

    print(f"Server starting on {parsed_args.host}:{parsed_args.port}")
    print(f"Telemetry exported via OTLP to: {parsed_args.otlp_endpoint}")
    print("Will send messages every second...")
    print("-" * 60)

    i = 0
    try:
        while True:
            time.sleep(1.0)

            # Create a span for this operation
            with tracer.start_as_current_span("send_message") as span:
                span.set_attribute("message_id", i)
                
                # Emit a log record
                log_record = OTelLogRecord(
                    timestamp=time.time_ns(),
                    body=f"Server message #{i}",
                    severity_text="INFO",
                    severity_number=SeverityNumber.INFO,
                    attributes={"message_id": i},
                )
                logger.emit(log_record)

                print(f"[SENDING] Message #{i}")

                # Send data over WebSocket
                sender.on_next(TaggedData("/", f"Server message #{i}"))

            i += 1

    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
        sender.on_completed()
        tracer_provider.shutdown()
        logger_provider.shutdown()
