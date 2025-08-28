import argparse
import time

from rxplus import RxWSServer, NamedLogComp, TaggedData


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("wsserver", help="start the ws server.")
    parser.add_argument("--host", type=str, default="::")
    parser.add_argument("--port", type=int, default=8888)
    parser.set_defaults(func=task)

def task(parsed_args: argparse.Namespace):
    sender = RxWSServer(
        {
            'host' : parsed_args.host, 
            'port' : parsed_args.port,
        }, 
        logcomp=NamedLogComp("RxWSServer"),
        datatype='string'
    )
    sender.subscribe(print)

    i = 0
    try:
        while True:
            time.sleep(1.0)
            sender.on_next(TaggedData("/", f"Hello {i}"))
            i += 1
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
