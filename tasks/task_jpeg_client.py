from typing import Literal, get_args
import reactivex as rx
from reactivex.scheduler.eventloop import AsyncIOScheduler
from reactivex import Subject
from reactivex import operators as ops
import numpy as np
import argparse

import asyncio

import rxplus
from rxplus import save_wavfile, NamedLogComp, drop_log, log_filter, RxWSClient, jpeg_bytes_to_rgb_ndarray
from reactivex.scheduler import ThreadPoolScheduler


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("jpeg_client", help="start the jpeg client.")
    # parser.add_argument("--path", type=str, default="output.wav")
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8888)
    parser.set_defaults(func=task)

def task(parsed_args: argparse.Namespace):

    async def test_jpeg_client():

        client = RxWSClient(
            {
                'host' : parsed_args.host, 
                'port' : parsed_args.port,
            }, 
            logcomp=NamedLogComp("RxWSClient"),
            datatype='bytes'
        )

        client.pipe(
            log_filter()
        ).subscribe(print)

        client.pipe(
            drop_log(),
            ops.map(jpeg_bytes_to_rgb_ndarray),
            ops.do_action(lambda x: print(f"Received image with shape: {x.shape}"))
        ).subscribe()
        
        await asyncio.Event().wait()  # run forever

    try:
        asyncio.run(test_jpeg_client())
        
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
