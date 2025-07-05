# RxPlus

This is a project to extend the capability of ReactiveX with more observables and operators.

## Install
To install the latest version from GitHub, run the following command:
```
pip install git+https://github.com/LucianoXu/rxplus.git
```

To install directly from the source, enter the project root and execute:
```
python -m build && pip install .
```

## Content

#### Python objects

- LogItem 
- LOG_LEVEL
- WSDatatype
- WSPyObj
- WSStr

#### operator
- stream_print_out
- log_filter
- drop_log
- log_redirect_to
- redirect_to

#### observable
- Logger
- RxWSServer
- RxWSClient