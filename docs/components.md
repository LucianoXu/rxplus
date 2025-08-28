# Component Documentation

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

Implementation note: the websocket internals now run on dedicated background threads, each with its own asyncio event loop. This means you can instantiate `RxWSServer`/`RxWSClient` from regular (non-async) code without managing an event loop; the Observable/Observer interface remains unchanged.

### Audio

The `audio` module contains helpers for working with sound. `RxMicrophone` and
`RxSpeaker` stream audio from the microphone or to the speaker. The
`create_wavfile` observable loads a WAV file chunk by chunk, while the
`save_wavfile` observer makes it easy to record audio streams.

### CLI

The `cli` module provides the `from_cli` operator, which creates an observable that emits strings from the command-line interface. This is useful for creating interactive command-line applications.
