"""Running blocking work off the GUI thread and getting the result back onto it."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

log = logging.getLogger(__name__)


def safe_emit(signal: Any, *args: Any) -> None:
    """Emit from a worker thread. If the receiving side was torn down meanwhile, drop the result."""
    try:
        signal.emit(*args)
    except RuntimeError:  # the QObject that owns the signal is already deleted
        pass


class _Relay(QObject):
    """Lives on the GUI thread; a worker emits on it, so delivery is queued onto the GUI thread."""

    done = Signal(object)
    failed = Signal(object)

    def __init__(self, parent: QObject, on_done: Callable | None, on_error: Callable | None) -> None:
        super().__init__(parent)
        self._on_done = on_done
        self._on_error = on_error
        self.done.connect(self._deliver_done)
        self.failed.connect(self._deliver_error)

    @Slot(object)
    def _deliver_done(self, result: object) -> None:
        try:
            if self._on_done is not None:
                self._on_done(result)
        finally:
            self._release()

    @Slot(object)
    def _deliver_error(self, exc: object) -> None:
        try:
            if self._on_error is not None:
                self._on_error(exc)
        finally:
            self._release()

    def _release(self) -> None:
        runner = self.parent()
        if isinstance(runner, TaskRunner):
            runner._live.discard(self)
        self._on_done = self._on_error = None
        self.deleteLater()


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any], relay: _Relay) -> None:
        super().__init__()
        self._fn = fn
        self._relay = relay

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # delivered to on_error on the GUI thread
            log.debug("background task failed", exc_info=True)
            safe_emit(self._relay.failed, exc)
        else:
            safe_emit(self._relay.done, result)
        finally:
            self._fn = None


class TaskRunner(QObject):
    """A small QThreadPool front end. Callbacks run on the GUI thread; results that arrive after the
    runner (or its parent window) is destroyed are dropped instead of crashing."""

    def __init__(self, parent: QObject | None = None, max_threads: int = 4) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max_threads)
        self._live: set[_Relay] = set()

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        **kwargs: Any,
    ) -> None:
        relay = _Relay(self, on_done, on_error)
        self._live.add(relay)
        self.pool.start(_Task(lambda: fn(*args, **kwargs), relay))

    def busy(self) -> bool:
        return bool(self._live)

    def shutdown(self, timeout_ms: int = 5000) -> None:
        self.pool.clear()
        self.pool.waitForDone(timeout_ms)
