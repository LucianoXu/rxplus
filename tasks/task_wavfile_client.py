from typing import Literal, get_args
import reactivex as rx
from reactivex import Subject
from reactivex import operators as ops
import numpy as np
import argparse
import time

import rxplus
from rxplus import save_wavfile, RxWSClient
from reactivex.scheduler import ThreadPoolScheduler


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("wavfile_client", help="start the wavfile client.")
    parser.add_argument("--path", type=str, default="output.wav")
    parser.add_argument("--format", help="the format of sound", type=str, choices=get_args(rxplus.PCMFormat), default="Float32")
    parser.add_argument("--sr", type=int, help="target sampling rate", default=48000)
    parser.add_argument("--ch", type=int, help="target channel number", default=1)
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8888)
    parser.set_defaults(func=task)

def task(parsed_args: argparse.Namespace):
    client = RxWSClient(
        {
            'host' : parsed_args.host, 
            'port' : parsed_args.port,
        }, 
        datatype='bytes'
    )

    wavfile = save_wavfile(
        path=parsed_args.path,
        format=parsed_args.format,
        sample_rate=parsed_args.sr,
        channels=parsed_args.ch
    )

    # Subscribe to write audio to wav file
    client.subscribe(wavfile)

    # Subscribe for error handling
    client.subscribe(
        on_error=lambda e: print(f"Error: {e}"),
        on_completed=lambda: print("Client completed"),
    )

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
