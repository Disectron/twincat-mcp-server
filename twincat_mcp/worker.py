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
import time
from typing import Any, Callable


class WorkerBusy(TimeoutError):
    """R6: the call never started — the worker was still busy with another job when the timeout ran out."""

    def __init__(self, worker: str, job: str, busy_with: str | None, busy_s: float | None, queued: int):
        self.worker, self.job, self.busy_with, self.busy_s, self.queued = worker, job, busy_with, busy_s, queued
        super().__init__(f"{job} not started: {worker} is busy with {busy_with or 'another job'}"
                         + (f" for {busy_s:.0f} s" if busy_s is not None else "") + (f", {queued} more queued" if queued else ""))


class SerialWorker:
    def __init__(self, name: str, init: Callable[[], None] | None = None,
                 teardown: Callable[[], None] | None = None,
                 cancel_event: threading.Event | None = None):
        self.name = name
        self.current: tuple[str, float] | None = None      # (job name, monotonic start) of the running job
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
                self.current = (getattr(fn, "__name__", "job"), time.monotonic())
                try:
                    fut.set_result(fn(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001
                    fut.set_exception(exc)
                finally:
                    self.current = None
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
            af = asyncio.wrap_future(fut)
            af.add_done_callback(lambda f: f.cancelled() or f.exception())    # retrieved: no "never retrieved" noise
            return await asyncio.wait_for(asyncio.shield(af), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            # R6: a job that never started is withdrawn (it must not run later, unobserved) and the caller learns
            # what the worker is busy with; only a job that is running gets the cooperative stop request — a queued
            # job's timeout used to cancel whatever else was running
            if fut.cancel():
                cur = self.current
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise WorkerBusy(self.name, getattr(fn, "__name__", "job"), cur[0] if cur else None,
                                 (time.monotonic() - cur[1]) if cur else None, self._q.qsize()) from None
            if fut.done() and not isinstance(exc, asyncio.CancelledError):
                return fut.result()          # finished right at the timeout: the answer (or its error) is there
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
