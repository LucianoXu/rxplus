import argparse
import time

from rxplus import RxWSClient, NamedLogComp


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("wsclient", help="start the ws client.")
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--path", type=str, default="/")
    parser.set_defaults(func=task)

def task(parsed_args: argparse.Namespace):
    receiver = RxWSClient(
        {
            'host' : parsed_args.host, 
            'port' : parsed_args.port,
            'path' : parsed_args.path,
        },
        logcomp=NamedLogComp("RxWSReceiver"),
        datatype='string')
    receiver.subscribe(print, on_error=print)

    i = 0
    try:
        while True:
            time.sleep(0.5)
            receiver.on_next(f"Ping {i}")
            i += 1
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
