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
or
```
conda install -c conda-forge portaudio
```

## Principles
- About exception handling: normal exceptions will be catched and passed on through `on_error`. The node that generates the error is responsible for annotating the environment of the error and wrapping it using `RxException`.
- Error observed by `on_error` should be handled or passed on directly.

## TODO Next 3
- Check the code and make sure that error will not be raised in rx nodes, but pushed forward.


## Development
Run the test suite with `pytest` after installing the optional `dev` dependencies.
