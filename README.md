# rxplus

`rxplus` is a Python library that provides a collection of extensions for the `reactivex` library. It offers various components to simplify reactive programming, including utilities for WebSocket communication, telemetry, and more.

## Installation

The package supports modular installation with optional dependencies for audio and video features.

### Basic Installation (core features only)
```bash
pip install git+https://github.com/LucianoXu/rxplus.git
```

### Installation with Optional Features
```bash
# Audio support (microphone, speaker, wav files)
pip install "git+https://github.com/LucianoXu/rxplus.git#egg=rxplus[audio]"

# Video support (screen capture, image conversion)
pip install "git+https://github.com/LucianoXu/rxplus.git#egg=rxplus[video]"

# All features
pip install "git+https://github.com/LucianoXu/rxplus.git#egg=rxplus[all]"
```

### Install from Source
```bash
python -m build && pip install .          # Basic only
pip install -e ".[audio]"                 # With audio support
pip install -e ".[video]"                 # With video support
pip install -e ".[all]"                   # All features
```

### System Dependencies for Audio
The `pyaudio` module requires `portaudio` installed on your system.

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install portaudio19-dev -y
```

**macOS (Homebrew):**
```bash
brew install portaudio
```

**Conda:**
```bash
conda install -c conda-forge portaudio
```

## Quick Start

### WebSocket Server with Telemetry

```python
from rxplus import RxWSServer, TaggedData, configure_telemetry

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

# Configure telemetry (optional)
tracer_provider, logger_provider = configure_telemetry(
    log_processor=BatchLogRecordProcessor(
        OTLPLogExporter(endpoint="http://localhost:4318/v1/logs")
    ),
)

# Create server with telemetry
server = RxWSServer(
    {"host": "::", "port": 8888},
    tracer_provider=tracer_provider,
    logger_provider=logger_provider,
)

# Subscribe to handle incoming data
server.subscribe(on_next=lambda x: print(f"Received: {x}"))

# Send data to clients
server.on_next(TaggedData("/", "Hello, clients!"))
```

### WebSocket Client

```python
from rxplus import RxWSClient

client = RxWSClient({"host": "localhost", "port": 8888})

# Subscribe to receive data from server
client.subscribe(on_next=lambda x: print(f"Received: {x}"))

# Send data to server
client.on_next("Hello, server!")
```

## Principles
- About exception handling: normal exceptions will be catched and passed on through `on_error`. The node that generates the error is responsible for annotating the environment of the error and wrapping it using `RxException`.
- Error observed by `on_error` should be handled or passed on directly.

## Documentation

- [Telemetry Integration](docs/telemetry.md) - How to configure OpenTelemetry for observability
- [WebSocket Components](docs/websocket.md) - RxWSServer, RxWSClient, RxWSClientGroup
- [Operators](docs/operators.md) - Custom RxPY operators


## Development
Run the test suite with `pytest` after installing the optional `dev` dependencies.
