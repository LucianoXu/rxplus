
import reactivex as rx
from reactivex import Observable
from reactivex.disposable import Disposable
from reactivex.subject import Subject

def from_cli():
    """
    Creates an observable that emits strings from the command line interface.
    """
    def _from_cli(source):
        def subscribe(observer, scheduler=None):

            def on_next(value):
                try:
                    print("Request: ", value)
                    print("> ", end="", flush=True)
                    line = input()
                    observer.on_next(line)

                except Exception as e:
                    observer.on_error(e)

            return source.subscribe(
                on_next=on_next,
                on_error=observer.on_error,
                on_completed=observer.on_completed,
                scheduler=scheduler
            )


        return Observable(subscribe)
    
    return _from_cli