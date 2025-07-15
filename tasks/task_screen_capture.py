import asyncio
import threading
import reactivex as rx
from reactivex.scheduler.eventloop import AsyncIOScheduler
from reactivex import operators as ops

import argparse

import time

from rxplus import create_screen_capture, FPSMonitor



def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("screen_capture", help="try the screen_capture operator")
    parser.add_argument("--fps", type=float, default=10.0, help="The frames per second for screen capture.")
    parser.set_defaults(func=task)

def task(parsed_args: argparse.Namespace):

    async def test_screen_capture():
        
        source = create_screen_capture(
            fps=parsed_args.fps
        )

        fps_monitor = FPSMonitor(interval=1.0)

        source.pipe(
            ops.map(fps_monitor),
            ops.do_action(lambda frame: print(f"Captured frame of shape: {frame.shape}")),
        ).subscribe()

        await asyncio.Event().wait()  # run forever

    try:
        asyncio.run(test_screen_capture())

    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
