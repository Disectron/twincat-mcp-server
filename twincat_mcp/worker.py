"""Single-thread executors.

Both pyads connections and COM objects are thread-affine. Every backend owns
one ``SerialWorker`` and the async tool layer awaits ``worker.run(fn, ...)``.
For COM the worker thread is initialised as an STA apartment.
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import queue
import threading
from typing import Any, Callable


class SerialWorker:
    def __init__(self, name: str, init: Callable[[], None] | None = None,
                 teardown: Callable[[], None] | None = None,
                 cancel_event: threading.Event | None = None):
        self.cancel_event = cancel_event or threading.Event()
        self._q: "queue.Queue[tuple[cf.Future, Callable, tuple, dict] | None]" = queue.Queue()
        self._init = init
        self._teardown = teardown
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._started = threading.Event()
        self._init_error: BaseException | None = None
        self._thread.start()
        self._started.wait()

    def _loop(self) -> None:
        try:
            if self._init:
                self._init()
        except BaseException as exc:  # noqa: BLE001
            self._init_error = exc
        finally:
            self._started.set()
        while True:
            item = self._q.get()
            if item is None:
                break
            fut, fn, args, kwargs = item
            if fut.set_running_or_notify_cancel():
                self.cancel_event.clear()  # a stale cancel from a timed-out job must not hit this one
                try:
                    fut.set_result(fn(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001
                    fut.set_exception(exc)
        if self._teardown:
            try:
                self._teardown()
            except BaseException:  # noqa: BLE001
                pass

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> cf.Future:
        if self._init_error:
            f: cf.Future = cf.Future()
            f.set_exception(self._init_error)
            return f
        f = cf.Future()
        self._q.put((f, fn, args, kwargs))
        return f

    async def run(self, fn: Callable[..., Any], *args: Any, timeout: float | None = None,
                  **kwargs: Any) -> Any:
        fut = self.submit(fn, *args, **kwargs)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # the job keeps the thread; ask cooperative loops (watch/wait/poll) to stop
            if not fut.done():
                self.cancel_event.set()
            raise

    def call(self, fn: Callable[..., Any], *args: Any, timeout: float | None = None,
             **kwargs: Any) -> Any:
        """Blocking variant for use from other worker threads / tests. On timeout the job is
        cancelled if it has not started (so it cannot run later, unobserved) and cooperative
        loops are asked to stop if it has."""
        fut = self.submit(fn, *args, **kwargs)
        try:
            return fut.result(timeout=timeout)
        except cf.TimeoutError:
            if not fut.cancel():
                self.cancel_event.set()
            raise TimeoutError(f"{getattr(fn, '__name__', 'job')} on {self.name} exceeded {timeout} s") from None

    def close(self) -> None:
        self._q.put(None)
