import argparse
import time
from typing import get_args

import rxplus
from rxplus import RxMicrophone, RxWSServer, tag

# TODO: problem encounted when changing the microphone device.
# The following code observed:
# ||PaMacCore (AUHAL)|| Error on line 2523: err='-50', msg=Unknown Error


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("mic_server", help="start the microphone node.")
    parser.add_argument(
        "--format",
        help="the format of sound",
        type=str,
        choices=get_args(rxplus.PCMFormat),
        default="Float32",
    )
    parser.add_argument("--sr", type=int, help="target sampling rate", default=48000)
    parser.add_argument("--ch", type=int, help="target channel number", default=1)
    parser.add_argument("--host", type=str, default="::")
    parser.add_argument("--port", type=int, default=8888)
    parser.set_defaults(func=task)


def task(parsed_args: argparse.Namespace):
    sender = RxWSServer(
        {
            "host": parsed_args.host,
            "port": parsed_args.port,
        },
        datatype="bytes",
    )

    mic = RxMicrophone(
        format=parsed_args.format,
        sample_rate=parsed_args.sr,
        channels=parsed_args.ch,
    )

    mic.pipe(
        tag("/"),
    ).subscribe(sender)

    # Subscribe to server for error handling
    sender.subscribe(
        on_error=lambda e: print(f"Error: {e}"),
        on_completed=lambda: print("Server completed"),
    )

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
