from typing import Literal, get_args
import asyncio
import reactivex as rx
from reactivex.scheduler.eventloop import AsyncIOScheduler
from reactivex import operators as ops
import pyaudio

import argparse

import rxplus
from rxplus import RxSpeaker, NamedLogComp, log_filter, drop_log, RxWSClient
from reactivex.scheduler import ThreadPoolScheduler

def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("speaker_client", help="start the speaker client.")
    parser.add_argument("--format", help="the format of sound", type=str, choices=get_args(rxplus.PCMFormat), default="Float32")
    parser.add_argument("--sr", type=int, help="target sampling rate", default=48000)
    parser.add_argument("--ch", type=int, help="target channel number", default=1)
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8888)
    parser.set_defaults(func=task)

def task(parsed_args: argparse.Namespace):

    async def test_speaker_server():

        client = RxWSClient(
            {
                'host' : parsed_args.host, 
                'port' : parsed_args.port,
            }, 
            logcomp=NamedLogComp("RxWSClient"),
            datatype='byte'
        )

        speaker = RxSpeaker(
            format = parsed_args.format,
            sample_rate = parsed_args.sr,
            channels=parsed_args.ch,
        )

        client.pipe(
            log_filter()
        ).subscribe(print)

        client.pipe(
            drop_log()
        ).subscribe(speaker)

        while True:
            await asyncio.sleep(1)

    try:
        asyncio.run(test_speaker_server())
        
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
