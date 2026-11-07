"""BoundedBuffer replaces DropOld, DropNew, BufferK and Disconnect (audit F21)."""

import threading

import pytest

from rxplus.gateway import BoundedBuffer, DisconnectError, create_overflow_policy


def test_drop_old_returns_the_dropped_item():
    buf = BoundedBuffer[int](3, "drop_old")
    assert [buf.push(i) for i in range(4)] == [None, None, None, 0]
    assert buf.clear() == [1, 2, 3]


def test_drop_new_refuses_the_new_item():
    buf = BoundedBuffer[int](2, "drop_new")
    assert [buf.push(i) for i in range(3)] == [None, None, 2]
    assert buf.clear() == [0, 1]


def test_buffer_k_and_disconnect_raise():
    k = BoundedBuffer[int](1, "buffer_k")
    k.push(1)
    with pytest.raises(OverflowError):
        k.push(2)
    d = BoundedBuffer[int](1, "disconnect")
    d.push(1)
    with pytest.raises(DisconnectError):
        d.push(2)


def test_pop_raises_on_empty_and_keeps_none_items():
    buf = BoundedBuffer[int | None](2)
    with pytest.raises(IndexError):
        buf.pop()
    buf.push(None)  # 0.1's pop() returned None for empty too: ambiguous
    assert buf.pop() is None
    with pytest.raises(IndexError):
        buf.peek()


def test_validation_and_factory():
    with pytest.raises(ValueError):
        BoundedBuffer(0)
    with pytest.raises(ValueError):
        BoundedBuffer(1, "bogus")  # type: ignore[arg-type]
    buf = create_overflow_policy("drop_new", buffer_limit=5)
    assert buf.capacity() == 5 and buf.strategy == "drop_new"


def test_thread_safe():
    buf = BoundedBuffer[int](10_000, "drop_new")

    def push() -> None:
        for i in range(1000):
            buf.push(i)

    threads = [threading.Thread(target=push) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(buf) == 8000
