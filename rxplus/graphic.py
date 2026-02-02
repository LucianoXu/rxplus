"""Graphic helpers built on top of ReactiveX."""

import asyncio
import time
from typing import Any, Optional, Union

from io import BytesIO

import numpy as np
import mss
import reactivex as rx
from PIL import Image
from reactivex import Observable
from reactivex import operators as ops
from reactivex.disposable import CompositeDisposable, Disposable
from reactivex.scheduler import ThreadPoolScheduler
from reactivex.scheduler.eventloop import AsyncIOScheduler

from .mechanism import RxException

def create_screen_capture(
    fps: float = 10.0,
    monitor: Union[int, dict, None] = None,
    scheduler: Optional[rx.abc.SchedulerBase] = None,
) -> Observable[np.ndarray]:
    """
    Create an observable that captures the screen at a specified FPS.
    The observable will adjust the time between frames to match the desired FPS.

    Args:
        fps (float): Frames per second for capturing the screen.
        monitor: Monitor selection:
            - None: Primary monitor (default, backwards compatible)
            - int: Monitor index (0=all monitors combined, 1=primary, 2+=secondary)
            - dict: Region specification {"left": x, "top": y, "width": w, "height": h}

    Output stream: 
        Observable emitting NumPy arrays representing RGB frames of the screen.
    """

    interval = 1.0 / fps

    sleep_time = interval  # sleep time between frames, intially set to the interval

    # control the time between frames
    previous_time = 0.5 * interval

    def subscribe(
        observer: rx.abc.ObserverBase, scheduler_: Optional[rx.abc.SchedulerBase] = None
    ) -> rx.abc.DisposableBase:

        try:
            loop = asyncio.get_running_loop()
            running = loop.is_running()
        except RuntimeError:
            loop = None
            running = False

        _scheduler = (
            scheduler
            or scheduler_
            or (
                AsyncIOScheduler(loop)  # type: ignore[assignment]
                if running
                else ThreadPoolScheduler(1)
            )
        )

        disposed = False


        async def _async_loop(_: rx.abc.SchedulerBase, __: Any) -> None:
            """Capture loop that uses asyncio.sleep so the event‑loop is never blocked."""
            nonlocal disposed, previous_time, sleep_time

            try:
                with mss.mss() as sct:
                    # Select monitor based on parameter type
                    if monitor is None:
                        _monitor = sct.monitors[1]  # primary monitor
                    elif isinstance(monitor, int):
                        _monitor = sct.monitors[monitor]
                    else:
                        _monitor = monitor  # dict region

                    while not disposed and True:
                        rgb = np.array(sct.grab(_monitor))[:, :, :3][:, :, ::-1]  # BGRA -> RGB
                        observer.on_next(rgb)
                        # adjust the sleep time based on the interval. experiments show that this is critical.
                        current_time = time.time()
                        elapsed = current_time - previous_time
                        sleep_time += 0.5 * (interval - elapsed)
                        previous_time = current_time
                        # sleep for the adjusted time
                        if sleep_time > 0:
                            await asyncio.sleep(sleep_time)
                        else:
                            sleep_time = 0.
                        
            except Exception as error:
                observer.on_error(RxException(
                    error, note=f"Error while capturing screen")
                )

        def _sync_loop(_: rx.abc.SchedulerBase, __: Any) -> None:
            """Capture loop that runs in a worker thread using blocking time.sleep."""
            nonlocal disposed, previous_time, sleep_time

            try:
                with mss.mss() as sct:
                    # Select monitor based on parameter type
                    if monitor is None:
                        _monitor = sct.monitors[1]  # primary monitor
                    elif isinstance(monitor, int):
                        _monitor = sct.monitors[monitor]
                    else:
                        _monitor = monitor  # dict region

                    while not disposed and True:
                        rgb = np.array(sct.grab(_monitor))[:, :, :3][:, :, ::-1]  # BGRA -> RGB
                        observer.on_next(rgb)
                        # adjust the sleep time based on the interval. experiments show that this is critical.
                        current_time = time.time()
                        elapsed = current_time - previous_time
                        sleep_time += 0.5 * (interval - elapsed)
                        previous_time = current_time
                        # sleep for the adjusted time
                        if sleep_time > 0:
                            time.sleep(sleep_time)
                        else:
                            sleep_time = 0.
                        
            except Exception as error:
                observer.on_error(RxException(
                    error, note=f"Error while capturing screen")
                )
            
        def dispose() -> None:
            nonlocal disposed
            disposed = True

        # scheduling
        if isinstance(_scheduler, AsyncIOScheduler):
            # spawn async task on the current event‑loop
            assert loop is not None, "AsyncIOScheduler requires an event loop"
            task = loop.create_task(_async_loop(_scheduler, None))
            capture_disp: rx.abc.DisposableBase = Disposable(lambda: (task.cancel(), None)[1])
        else:
            # run in a worker thread
            capture_disp = _scheduler.schedule(_sync_loop)
        
        return CompositeDisposable(capture_disp, Disposable(dispose))

    return Observable(subscribe)


def rgb_ndarray_to_jpeg_bytes(frame: np.ndarray, quality: int = 80) -> bytes:
    """
    Convert RGB NumPy array to JPEG bytes.

    Parameters
    ----------
    frame : np.ndarray
        Image as RGB array (H, W, 3), dtype uint8.
    quality : int, optional
        JPEG quality (0-100), by default 80.

    Returns
    -------
    bytes
        JPEG encoded image data.
    """
    
    width, height = frame.shape[1], frame.shape[0]

    img = Image.frombytes("RGB", (width, height), frame.tobytes())

    # Convert to JPEG format with specified quality
    with BytesIO() as output:
        img.save(output, format="JPEG", quality=quality)
        jpeg_data = output.getvalue()

    return jpeg_data

def jpeg_bytes_to_rgb_ndarray(jpeg: bytes) -> np.ndarray:
    """
    Convert JPEG bytes to H×W×3 uint8 NumPy array (RGB).

    Parameters
    ----------
    jpeg : bytes
        Raw JPEG data.

    Returns
    -------
    np.ndarray
        Image as RGB array (copy, contiguous).
    """
    with Image.open(BytesIO(jpeg)) as im:
        rgb = im.convert("RGB")        # ensure 3-channel
        return np.asarray(rgb)         # shape (H, W, 3), dtype uint8


def rgb_ndarray_to_png_bytes(frame: np.ndarray, compression: int = 6) -> bytes:
    """
    Convert RGB NumPy array to lossless PNG bytes.

    Parameters
    ----------
    frame : np.ndarray
        Image as RGB array (H, W, 3), dtype uint8.
    compression : int, optional
        PNG compression level (0-9, where 9 is maximum compression), by default 6.

    Returns
    -------
    bytes
        PNG encoded image data.
    """
    width, height = frame.shape[1], frame.shape[0]

    img = Image.frombytes("RGB", (width, height), frame.tobytes())

    # Convert to PNG format with specified compression
    with BytesIO() as output:
        img.save(output, format="PNG", compress_level=compression)
        png_data = output.getvalue()

    return png_data


def png_bytes_to_rgb_ndarray(png: bytes) -> np.ndarray:
    """
    Convert PNG bytes to H×W×3 uint8 NumPy array (RGB).

    Parameters
    ----------
    png : bytes
        Raw PNG data.

    Returns
    -------
    np.ndarray
        Image as RGB array (copy, contiguous).
    """
    with Image.open(BytesIO(png)) as im:
        rgb = im.convert("RGB")        # ensure 3-channel
        return np.asarray(rgb)         # shape (H, W, 3), dtype uint8