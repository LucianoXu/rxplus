import argparse
import numpy as np
import time

from rxplus import RxWSClient


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser(
        "np_matrix_client", help="WebSocket client for small numpy matrices (object frames)."
    )
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--path", type=str, default="/matrix")
    parser.add_argument("--rows", type=int, default=4)
    parser.add_argument("--cols", type=int, default=4)
    parser.add_argument(
        "--dtype",
        type=str,
        default="float32",
        choices=["float32", "float64", "int16", "int32"],
    )
    parser.add_argument("--tx_fps", type=float, default=1.0, help="Transmit matrices per second")
    parser.add_argument("--seed", type=int, default=0, help="Local random seed for transmitted matrices")
    parser.set_defaults(func=task)


def task(parsed_args: argparse.Namespace):
    client = RxWSClient(
        {
            "host": parsed_args.host,
            "port": parsed_args.port,
            "path": parsed_args.path,
        },
        datatype="object",
    )

    # Print inbound matrices
    def on_next(arr):
        if isinstance(arr, np.ndarray):
            print(f"Received array shape={arr.shape} dtype={arr.dtype} sum={arr.sum():.3f}")
        else:
            print(f"Received (non-ndarray): {type(arr)} -> {arr}")

    client.subscribe(on_next, on_error=print)

    # Periodically transmit matrices to the server as a demo
    rng = np.random.default_rng(parsed_args.seed)
    dt = 1.0 / max(parsed_args.tx_fps, 1e-6)
    rows, cols = parsed_args.rows, parsed_args.cols
    dtype = np.dtype(parsed_args.dtype)


    try:    
        while True:
            time.sleep(dt)
            if np.issubdtype(dtype, np.floating):
                arr = rng.random((rows, cols), dtype=dtype)
            else:
                arr = rng.integers(low=0, high=100, size=(rows, cols), dtype=dtype)
            client.on_next(arr)
            
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
