"""Command-line interface utilities."""

import asyncio
from typing import Callable, Literal

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from reactivex import Observable


def to_cli(prefix: str = "") -> Callable[[Observable], Observable]:
    """
    Display upstream items to terminal without interrupting active prompts.
    
    Uses prompt_toolkit's patch_stdout() for safe interleaving with from_cli().
    Values pass through unchanged downstream.
    
    Parameters
    ----------
    prefix : str, default ""
        String to prepend to each displayed item.
    
    Returns
    -------
    Callable[[Observable], Observable]
        An operator that can be used with pipe().
    
    Example
    -------
    >>> source.pipe(
    ...     to_cli(prefix="[recv] ")
    ... ).subscribe()
    """
    def _to_cli(source: Observable) -> Observable:
        def subscribe(observer, scheduler=None):
            def on_next(value):
                with patch_stdout():
                    print(f"{prefix}{value}")
                observer.on_next(value)
            
            return source.subscribe(
                on_next=on_next,
                on_error=observer.on_error,
                on_completed=observer.on_completed,
                scheduler=scheduler,
            )
        return Observable(subscribe)
    return _to_cli


def from_cli(
    loop: asyncio.AbstractEventLoop | None = None,
    *,
    mode: Literal[
        "queue", "update", "loop"
    ] = "loop",  # True = queue every question; False = only keep the latest
):
    """
    Turn each element from the source Observable into an interactive prompt,
    forward the user's reply downstream, and avoid prompt-toolkit concurrency issues.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop | None
        Event loop to schedule tasks on.  If None, the current running loop is used.
    mode : Literal["queue", "update", "loop"], default "loop"
        * "queue"  – Every incoming value is queued; the user answers them in order.
                     If the user is already typing, the prompt text is NOT changed.
        * "update" – Only the most recent value is shown; older ones are discarded.
                     If the user is typing, the prompt text is simply replaced.
        * "loop"   – Continuously loop through prompts with the latest value.
    """

    if mode not in ["queue", "update", "loop"]:
        raise ValueError(
            f"Invalid mode: {mode}. Choose from 'queue', 'update', or 'loop'."
        )

    def _from_cli(source: Observable) -> Observable:

        def subscribe(observer, scheduler=None):
            # Runtime state ---------------------------------------------------
            session = PromptSession()
            _loop = loop or asyncio.get_running_loop()
            waiting_prompts = asyncio.Queue()  # All pending questions
            current_prompt = {"text": ""}  # Mutable reference for live updates
            awaiting_input = {"flag": False}  # True while prompt_async is waiting
            done = asyncio.Event()  # Signals graceful shutdown

            # Background coroutine: serially handles queued questions ----------
            async def prompt_loop():
                while not done.is_set():
                    if mode != "loop":
                        current_prompt["text"] = await waiting_prompts.get()
                    awaiting_input["flag"] = True
                    try:
                        with patch_stdout():
                            line = await session.prompt_async(
                                lambda: f"{current_prompt['text']}> "
                            )
                        observer.on_next(line)
                    except (EOFError, KeyboardInterrupt):
                        observer.on_completed()
                        done.set()
                    except Exception as exc:
                        observer.on_error(exc)
                        done.set()
                    finally:
                        awaiting_input["flag"] = False

            # Start the loop once
            _loop.create_task(prompt_loop())

            # Upstream on_next handler ----------------------------------------
            def _on_next(value):
                async def _handle():
                    if mode == "queue":
                        # Always queue the question; do not replace prompt while typing
                        await waiting_prompts.put(value)
                        if not awaiting_input["flag"] and session.app:
                            session.app.invalidate()
                    else:
                        if awaiting_input["flag"]:
                            # Replace current prompt, discard any queued ones
                            current_prompt["text"] = value
                            while not waiting_prompts.empty():
                                waiting_prompts.get_nowait()
                            session.app.invalidate()
                        else:
                            # No prompt active — queue and show immediately
                            await waiting_prompts.put(value)

                asyncio.run_coroutine_threadsafe(_handle(), _loop)

            # Subscribe to the upstream Observable
            return source.subscribe(
                on_next=_on_next,
                on_error=observer.on_error,
                on_completed=lambda: done.set(),
                scheduler=scheduler,
            )

        return Observable(subscribe)

    return _from_cli
