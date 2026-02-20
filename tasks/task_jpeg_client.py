import argparse
import time

from reactivex import operators as ops

from rxplus import RxWSClient, jpeg_bytes_to_rgb_ndarray


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("jpeg_client", help="start the jpeg client.")
    # parser.add_argument("--path", type=str, default="output.wav")
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8888)
    parser.set_defaults(func=task)


def task(parsed_args: argparse.Namespace):
    client = RxWSClient(
        {
            "host": parsed_args.host,
            "port": parsed_args.port,
        },
        datatype="bytes",
    )

    # Subscribe to process JPEG frames
    client.pipe(
        ops.map(jpeg_bytes_to_rgb_ndarray),
        ops.do_action(lambda x: print(f"Received image with shape: {x.shape}")),
    ).subscribe()

    # Subscribe for error handling
    client.subscribe(
        on_error=lambda e: print(f"Error: {e}"),
        on_completed=lambda: print("Client completed"),
    )

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
