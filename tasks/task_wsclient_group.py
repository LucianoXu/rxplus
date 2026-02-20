import argparse
import time

from rxplus import RxWSClientGroup, TaggedData


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("wsclient_group", help="start the ws client group.")
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--path", type=str, default="/")
    parser.set_defaults(func=task)


def task(parsed_args: argparse.Namespace):
    receiver = RxWSClientGroup(
        {
            "host": parsed_args.host,
            "port": parsed_args.port,
        },
        datatype="string",
    )

    receiver.subscribe(print, on_error=print)

    i = 0
    try:
        while True:
            time.sleep(0.5)
            receiver.on_next(TaggedData(parsed_args.path, f"Ping {i}"))
            i += 1
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
