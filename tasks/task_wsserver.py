import asyncio
import reactivex as rx
from reactivex.scheduler.eventloop import AsyncIOScheduler
from reactivex import operators as ops

from rxplus.ws import *

import argparse

import asyncio

from rxplus import RxWSServer, NamedLogComp, drop_log


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("wsserver", help="start the ws server.")
    parser.add_argument("--host", type=str, default="::")
    parser.add_argument("--port", type=int, default=8888)
    parser.set_defaults(func=task)

def task(parsed_args: argparse.Namespace):

    async def test_rxws_S():
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
        while True:
            await asyncio.sleep(1)
            sender.on_next("Hello " + str(i))
            i += 1

    try:
        asyncio.run(test_rxws_S())

    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")