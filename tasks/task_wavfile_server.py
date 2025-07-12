from typing import Literal, get_args
import reactivex as rx
from reactivex.scheduler.eventloop import AsyncIOScheduler
from reactivex import Subject
from reactivex import operators as ops
import numpy as np

import argparse

import asyncio

import threading
import time

from rxplus import create_wavfile, NamedLogComp, drop_log, RxWSServer
from reactivex.scheduler import ThreadPoolScheduler


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("wavfile_server", help="start the wavfile server.")
    parser.add_argument("--path", type=str, default="")
    parser.add_argument("--sr", type=int, help="target sampling rate", default=48000)
    parser.add_argument("--ch", type=int, help="target channel number", default=1)
    parser.add_argument("--host", type=str, default="::")
    parser.add_argument("--port", type=int, default=8888)
    parser.set_defaults(func=task)

def task(parsed_args: argparse.Namespace):

    async def test_microphone_server():

        sender = RxWSServer(
            {
                'host' : parsed_args.host, 
                'port' : parsed_args.port,
            }, 
            logcomp=NamedLogComp("RxWSServer"),
            datatype='byte'
        )

        # create the network with some cli output
        data = Subject()
        data.subscribe(lambda x: print(x[:5]))
        data.subscribe(sender)

        wavfile = create_wavfile(
            wav_path=parsed_args.path,
            target_sample_rate=parsed_args.sr,
            target_channels=parsed_args.ch
        ).pipe(
            ops.map(lambda d: d.tobytes()),
            ops.repeat()
        )

        # create the source
        wavfile.subscribe(data)

        while True:
            await asyncio.sleep(1)

    try:
        asyncio.run(test_microphone_server())
        
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
