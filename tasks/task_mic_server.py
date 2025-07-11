import asyncio
import reactivex as rx
from reactivex.scheduler.eventloop import AsyncIOScheduler
from reactivex import operators as ops

import argparse

import asyncio

import pyaudio

import threading
import time

from rxplus import RxMicrophone, NamedLogComp, log_filter, drop_log, RxWSServer
from reactivex.scheduler import ThreadPoolScheduler


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("mic_server", help="start the microphone node.")
    parser.add_argument("--format", help="the format of sound", type=str, default="16") # TODO: turn the type into Literal["32", "16"]
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


        if parsed_args.format == "16":
            format = pyaudio.paInt16
        elif parsed_args.format == "32":
            format = pyaudio.paFloat32
        else:
            raise ValueError(f"Unexpected format argument: {parsed_args.format}")
        
        mic = RxMicrophone(
            format = format,
            sample_rate = parsed_args.sr,
            channels = parsed_args.ch,
        )

        mic.subscribe(sender)

        sender.pipe(log_filter()).subscribe(print)

        while True:
            await asyncio.sleep(1)

    try:
        asyncio.run(test_microphone_server())
        
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
