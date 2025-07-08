import asyncio
import threading
import reactivex as rx
from reactivex.scheduler.eventloop import AsyncIOScheduler
from reactivex import operators as ops

import argparse

import time

from rxplus import RxWSServer, NamedLogComp, drop_log, from_cli


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("cli", help="try the cli operator")
    parser.set_defaults(func=task)

def task(parsed_args: argparse.Namespace):

    async def test_cli():
        
        # Create an observable that emits values every 2 seconds
        source = rx.interval(2.0)

        source.pipe(
            from_cli(preserve=False)
        ).subscribe(
            on_next=lambda value: print(f"Received from CLI: {value}\n"),
            on_error=lambda error: print(f"Error: {error}"),
            on_completed=lambda: print("CLI input completed")
        )

        while True:
            await asyncio.sleep(0.5)

    try:
        asyncio.run(test_cli())

    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
