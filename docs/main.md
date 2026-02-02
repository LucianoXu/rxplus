# Component Documentation

This directory contains detailed documentation for each `rxplus` module. The library extends [RxPY](https://github.com/ReactiveX/RxPY) with practical components for I/O, networking, and media streaming.

## Quick Navigation

| Module | Description |
|--------|-------------|
| [Audio](audio.md) | Microphone, speaker, WAV file I/O |
| [CLI](cli.md) | Interactive command-line input |
| [Duplex](duplex.md) | Bidirectional communication channels |
| [Operators](operators.md) | Custom Rx operators |
| [Telemetry](telemetry.md) | OpenTelemetry integration for observability |
| [Utilities](utilities.md) | Tagged data, monitors, helpers |
| [Video](video.md) | Screen capture and image encoding |
| [WebSocket](websocket.md) | Reactive WebSocket server/client |

## Design Philosophy

`rxplus` follows these principles:

1. **Subject-based I/O** — External resources (WebSocket, microphone, speaker) are exposed as `Subject`s, acting as both Observable and Observer. This allows bidirectional data flow with a unified API.

2. **Threading transparency** — Components that require async I/O (WebSocket, audio) manage their own background threads internally. Users interact via the standard Rx interface without needing `asyncio` knowledge.

3. **Tagged data multiplexing** — `TaggedData` enables routing multiple logical channels through a single stream, essential for multi-path WebSocket communication.

4. **Provider-based telemetry** — Components accept optional OTel provider parameters, enabling observability without tight coupling to any specific backend.
