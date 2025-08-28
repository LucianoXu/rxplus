import reactivex as rx
from reactivex.scheduler import ThreadPoolScheduler
from reactivex import operators as ops

import argparse
import time

from rxplus import create_screen_capture, RxWSServer, NamedLogComp, rgb_ndarray_to_jpeg_bytes, FPSMonitor, BandwidthMonitor, tag



def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser("screen_capture_server", help="try the screen_capture server.")
    parser.add_argument("--fps", type=float, default=10.0, help="The frames per second for screen capture.")
    parser.add_argument("--host", type=str, default="::")
    parser.add_argument("--port", type=int, default=8888)
    parser.set_defaults(func=task)

def task(parsed_args: argparse.Namespace):
    sender = RxWSServer(
        {
            'host' : parsed_args.host, 
            'port' : parsed_args.port,
        }, 
        logcomp=NamedLogComp("RxWSServer"),
        datatype='bytes'
    )

    source = create_screen_capture(
        fps=parsed_args.fps,
        scheduler=ThreadPoolScheduler(1)
    )

    fps_monitor = FPSMonitor(interval=1.0)
    bandwidth_monitor = BandwidthMonitor(interval=1.0, scale=1/(1024.0 * 1024.0), unit="MB/s")

    source.pipe(
        ops.do_action(lambda frame: print(f"Captured frame of shape: {frame.shape}")),
        ops.map(fps_monitor),
        ops.observe_on(ThreadPoolScheduler(1)),
        ops.map(lambda frame: rgb_ndarray_to_jpeg_bytes(frame, quality=80)),
        ops.map(bandwidth_monitor),
        tag("/"),
    ).subscribe(sender)

    sender.subscribe(print)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
