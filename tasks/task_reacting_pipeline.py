"""An rxplus pipeline that reports itself live to the Reacting hub.

A WebSocket server ("Frontend") parses, works on and answers JSON readings
sent by a few clients ("Sensors"). Every component reports through
``reacting=``; probes measure the user's own stages. Start the hub in the
Reacting repository (``npm run serve``), then:

    python -m tasks.task_reacting_pipeline              # live, into the hub
    python -m tasks.task_reacting_pipeline --record r.jsonl --duration 20

The hub records the run; ``conformance/check.py check --direct`` accepts
the recording.
"""

import argparse
import json
import random
import socket
import threading
import time
from dataclasses import dataclass

from reactivex import operators as ops
from reactivex.scheduler import ThreadPoolScheduler

from rxplus import RxWSClient, RxWSServer, TaggedData, WSConnectionConfig
from rxplus.reacting import JsonlFileSink, Reporter, Sink, WebSocketSink
from rxplus.reacting import rx as rr
from rxplus.reacting.handles import Element

HUB = "ws://127.0.0.1:8787/source"


@dataclass
class Options:
    hub: str = HUB
    token: str | None = None
    record: str | None = None
    duration: float | None = None
    sensors: int = 3
    rate: float = 20.0  # readings per second per sensor
    window: float = 0.1
    seed: int = 1


def build_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "reacting_pipeline",
        help="An rxplus pipeline reporting live to the Reacting hub.",
    )
    _add_arguments(parser)
    parser.set_defaults(func=task)


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hub", default=HUB, help="the hub's source address")
    parser.add_argument("--token", default=None, help="bearer token (remote hubs)")
    parser.add_argument("--record", default=None, help="write a .jsonl file instead")
    parser.add_argument("--duration", type=float, default=None, help="seconds")
    parser.add_argument("--sensors", type=int, default=3)
    parser.add_argument("--rate", type=float, default=20.0, help="per sensor, 1/s")
    parser.add_argument("--window", type=float, default=0.1, help="seconds")
    parser.add_argument("--seed", type=int, default=1)


def task(args: argparse.Namespace) -> None:
    run(
        Options(
            hub=args.hub,
            token=args.token,
            record=args.record,
            duration=args.duration,
            sensors=args.sensors,
            rate=args.rate,
            window=args.window,
            seed=args.seed,
        )
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _parse(td: TaggedData) -> TaggedData | None:
    try:
        reading = json.loads(td.data)
        return TaggedData(td.tag, (reading["sensor"], float(reading["value"])))
    except (ValueError, KeyError, TypeError):
        return None  # malformed: filtered out (the flow counts show it)


def _send_port(client: RxWSClient) -> Element:
    port = client.reacting_port("send")
    assert port is not None  # the client reports (reacting=)
    return port


class _Sensor:
    """A client that sends readings; it reconnects now and then, so its
    socket comes and goes in the graph."""

    def __init__(self, index: int, port: int, parent: Element, opts: Options) -> None:
        self.index = index
        self.port = port
        self.parent = parent
        self.opts = opts
        self.rng = random.Random(opts.seed * 100 + index)
        reporter = parent.reporter
        self.source = parent.child(f"Sensor {index}", kind="source")
        self.client = self._client()
        self.out = reporter.link(self.source, _send_port(self.client))

    def _client(self) -> RxWSClient:
        client = RxWSClient(
            WSConnectionConfig("127.0.0.1", self.port, f"/sensor{self.index}"),
            name=f"Client {self.index}",
            buffer_while_disconnected=True,
            reacting=self.parent,
        )
        client.subscribe(lambda _: None)  # answers
        return client

    def run(self, stop: threading.Event) -> None:
        period = 1.0 / self.opts.rate
        next_reconnect = time.monotonic() + 15 + 5 * self.index
        n = 0
        while not stop.wait(period * self.rng.uniform(0.5, 1.5)):
            n += 1
            bad = self.rng.random() < 0.05  # some malformed readings
            body = (
                "not json"
                if bad
                else json.dumps({"sensor": self.index, "value": self.rng.gauss(20, 3)})
            )
            self.out.deliver(1, bytes=len(body))
            self.client.on_next(body)
            if time.monotonic() >= next_reconnect:
                next_reconnect += 30
                self.client.on_completed()  # its elements leave the graph
                self.client = self._client()
                self.out.remove()
                self.out = self.source.reporter.link(
                    self.source, _send_port(self.client)
                )
        self.client.on_completed()


def run(opts: Options) -> None:
    sink: Sink
    if opts.record is not None:
        sink = JsonlFileSink(opts.record, overwrite=True)
    else:
        sink = WebSocketSink(opts.hub, token=opts.token)
    reporter = Reporter(
        "rxplus-pipeline", sink, label="rxplus pipeline", window=opts.window
    )
    port = _free_port()
    server = RxWSServer(
        WSConnectionConfig("127.0.0.1", port), name="Frontend", reacting=reporter
    )
    app = reporter.element("App", kind="pipeline")
    parse = app.child("Parse", kind="operator")
    work = app.child("Work", kind="operator")
    reply = app.child("Reply", kind="operator")
    recv, send = server.reacting_port("recv"), server.reacting_port("send")
    assert recv is not None and send is not None
    to_parse = reporter.link(recv, parse)
    to_work = reporter.link(parse, work, capacity=256)  # a thread hop
    to_reply = reporter.link(work, reply)
    to_send = reporter.link(reply, send)

    rng = random.Random(opts.seed)
    pool = ThreadPoolScheduler(2)

    def do_work(td: TaggedData) -> TaggedData:
        # Mostly fast, with slow spells every ten seconds: the hop backs up.
        slow = int(time.monotonic()) % 10 < 2
        time.sleep(rng.uniform(0.004, 0.02) if slow else rng.uniform(0.0005, 0.003))
        sensor, value = td.data
        return TaggedData(td.tag, json.dumps({"sensor": sensor, "ok": value > 0}))

    server.pipe(
        rr.count(to_parse),
        ops.map(_parse),
        ops.filter(lambda td: td is not None),
        rr.enter(to_work),
        ops.observe_on(rr.ContextScheduler(pool)),
        rr.leave(to_work),
        rr.work(work),
        ops.map(do_work),
        rr.count(to_reply),
        rr.count(to_send),
    ).subscribe(server)

    sensors_el = reporter.element("Sensors", kind="group")
    time.sleep(0.2)  # the server binds
    sensors = [_Sensor(i + 1, port, sensors_el, opts) for i in range(opts.sensors)]
    stop = threading.Event()
    threads = [
        threading.Thread(target=s.run, args=(stop,), daemon=True) for s in sensors
    ]
    for t in threads:
        t.start()
    where = opts.record if opts.record is not None else opts.hub
    print(f"reporting to {where}; Ctrl-C stops", flush=True)
    try:
        stop.wait(opts.duration)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for t in threads:
            t.join(2.0)
        server.on_completed()
        time.sleep(opts.window * 2)  # the last windows close, then removals
        reporter.close(timeout=5.0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _add_arguments(parser)
    task(parser.parse_args())
