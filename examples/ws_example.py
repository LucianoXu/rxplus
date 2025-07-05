import asyncio
import reactivex as rx
from reactivex.scheduler.eventloop import AsyncIOScheduler
from reactivex import operators as ops

from rxplus.ws import *

# this test demonstrates that the WSSender and WSReceiver can be used to send and receive messages across the network.

# run this on the server side
def server():
    async def test_rxws_S():
        sender = RxWSServer(
            {
                'host' : '0.0.0.0', 
                'port' : 8888,
            }, 
            logcomp=NamedLogComp("RxWSServer"),
            datatype='string'
        )
        sender.subscribe(print)
        
        i = 0
        while True:
            await asyncio.sleep(1)
            sender.on_next("Hello " + str(i))
            i += 1

    try:
        asyncio.run(test_rxws_S())

    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")


# run this on the client side
def client():
    async def test_rxws_C():
        receiver = RxWSClient(
            {
                'host' : 'localhost', 
                'port' : 8888,
                'path' : '//',
            },
            logcomp=NamedLogComp("RxWSReceiver"),
            datatype='string')
        receiver.subscribe(print, on_error=print)

        i = 0
        while True:
            await asyncio.sleep(0.5)
            receiver.on_next("Ping " + str(i))
            i += 1

    try:
        asyncio.run(test_rxws_C())

    except KeyboardInterrupt:
        print("\nKeyboard Interrupt.")

