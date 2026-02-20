import argparse
import time

import numpy as np

from rxplus import RxWSServer, TaggedData


def build_parser(subparsers: argparse._SubParsersAction):
    parser = subparsers.add_parser(
        "np_matrix_server",
        help="WebSocket server streaming small numpy matrices (object frames).",
    )
    parser.add_argument("--host", type=str, default="::")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--path", type=str, default="/matrix")
    parser.add_argument("--rows", type=int, default=4, help="Matrix rows")
    parser.add_argument("--cols", type=int, default=4, help="Matrix cols")
    parser.add_argument(
        "--dtype",
        type=str,
        default="float32",
        choices=["float32", "float64", "int16", "int32"],
        help="Matrix dtype",
    )
    parser.add_argument("--fps", type=float, default=2.0, help="Matrices per second")
    parser.add_argument("--seed", type=int, default=None, help="Random seed")
    parser.set_defaults(func=task)


def _make_random_matrix(
    rng: np.random.Generator, rows: int, cols: int, dtype: np.dtype
) -> np.ndarray:
    if np.issubdtype(dtype, np.floating):
        return rng.random((rows, cols), dtype=dtype)
    else:
        # Small range for demo integer matrices
        return rng.integers(low=0, high=100, size=(rows, cols), dtype=dtype)


def task(parsed_args: argparse.Namespace):

    sender = RxWSServer(
        {
            "host": parsed_args.host,
            "port": parsed_args.port,
        },
        datatype="object",
    )

    # Log inbound messages from clients (if any)
    sender.subscribe(lambda t: print(t))

    rng = np.random.default_rng(parsed_args.seed)
    dt = 1.0 / max(parsed_args.fps, 1e-6)
    rows, cols = parsed_args.rows, parsed_args.cols
    dtype = np.dtype(parsed_args.dtype)

    try:
        while True:
            time.sleep(dt)
            arr = _make_random_matrix(rng, rows, cols, dtype)
            sender.on_next(TaggedData(parsed_args.path, arr))

    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
