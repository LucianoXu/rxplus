# rxplus

`rxplus` is a Python library that provides a collection of extensions for the `reactivex` library. It offers various components to simplify reactive programming, including utilities for logging, WebSocket communication, and more.

## Installation

To install the latest version from GitHub, run the following command:
```
pip install git+https://github.com/LucianoXu/rxplus.git
```

To install directly from the source, enter the project root and execute:
```
python -m build && pip install .
```

The `pyaudio` module installation requires some configuration. For example, on `Ubuntu` it requires `portaudio` installed in advance. The corresponding shell command is:
```
sudo apt update
sudo apt install portaudio19-dev -y
```

## Components

### Duplex

The `duplex` module provides a `Duplex` class that represents a bidirectional communication channel. It consists of a `sink` for incoming data and a `stream` for outgoing data, both of which are `reactivex` Subjects.

### Logging

`rxplus` includes a flexible logging framework designed for reactive applications. It allows you to create loggers, filter log messages by level, and redirect logs to different observers.

### Operators

The `opt` module contains custom `reactivex` operators, such as `redirect_to`, which allows you to redirect items in a stream to a different observer based on a condition.

### Utilities

The `utils` module provides various utility functions, including `TaggedData` for wrapping data with a tag and error handling functions.

### WebSocket

The `ws` module offers a reactive wrapper around the `websockets` library, providing `RxWSServer` and `RxWSClient` classes for building real-time, bidirectional WebSocket applications.

### CLI

The `cli` module provides the `from_cli` operator, which creates an observable that emits strings from the command-line interface. This is useful for creating interactive command-line applications.

## Usage

Here is a simple example of how to use the WebSocket component in `rxplus`:

### Server

```python
import asyncio
from rxplus.ws import RxWSServer, NamedLogComp

async def main():
    server = RxWSServer(
        {
            'host': '0.0.0.0',
            'port': 8888,
        },
        logcomp=NamedLogComp("RxWSServer"),
        datatype='string'
    )
    server.subscribe(print)

    i = 0
    while True:
        await asyncio.sleep(1)
        server.on_next("Hello " + str(i))
        i += 1

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
```

### Client

```python
import asyncio
from rxplus.ws import RxWSClient, NamedLogComp

async def main():
    client = RxWSClient(
        {
            'host': 'localhost',
            'port': 8888,
            'path': '/',
        },
        logcomp=NamedLogComp("RxWSClient"),
        datatype='string'
    )
    client.subscribe(print, on_error=print)

    i = 0
    while True:
        await asyncio.sleep(0.5)
        client.on_next("Ping " + str(i))
        i += 1

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")
```
