import asyncio
import reactivex as rx
from reactivex.scheduler.eventloop import AsyncIOScheduler
from reactivex import operators as ops

from rxplus.ws import *

import argparse

import asyncio

from rxplus import RxWSClientGroup, NamedLogComp, drop_log


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("wsclient_group", help="start the ws client group.")
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--path", type=str, default="/")
    parser.set_defaults(func=task)

def task(parsed_args: argparse.Namespace):

    async def test_rxws_C():
        receiver = RxWSClientGroup(
            {
                'host' : parsed_args.host, 
                'port' : parsed_args.port,
            },
            logcomp=NamedLogComp("RxWSReceiver"),
            datatype='string')
        
        receiver.subscribe(print, on_error=print)

        i = 0
        while True:
            await asyncio.sleep(0.5)
            receiver.on_next(TaggedData(parsed_args.path, "Ping " + str(i)))
            i += 1

    try:
        asyncio.run(test_rxws_C())

    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")

