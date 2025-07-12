paUInt8 = 0x01
paInt16 = 0x02
paInt24 = 0x04
paInt32 = 0x08
paFloat32 = 0x10
paAbort = 2
paContinue = 0

class DummyStream:
    def start_stream(self):
        pass
    def stop_stream(self):
        pass
    def close(self):
        pass
    def is_active(self):
        return False

def _not_available(*args, **kwargs):
    raise RuntimeError("PyAudio functionality not available in test environment")

class PyAudio:
    def open(self, *args, **kwargs):
        return DummyStream()
    def terminate(self):
        pass
