# rxplus

`rxplus` is a Python library that provides a collection of extensions for the `reactivex` library. It offers various components to simplify reactive programming, including utilities for logging, WebSocket communication, and more.

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

## Principles
- About exception handling: normal exceptions will be catched and passed on through `on_error`. The node that generates the error is responsible for annotating the environment of the error and wrapping it using `RxException`.
- Error observed by `on_error` should be handled or passed on directly.

## TODO Next 3
- Check the code and make sure that error will not be raised in rx nodes, but pushed forward.


## Development
Run the test suite with `pytest` after installing the optional `dev` dependencies.
