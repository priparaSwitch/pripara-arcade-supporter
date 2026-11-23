"""Cooperative background indexing; never selects or sends a ticket."""
from pathlib import Path
import threading
import math
from PySide6.QtCore import QThread,Signal


class IndexWorker(QThread):
    changed = Signal(object)
    failed = Signal(str)

    def __init__(self, store, directory, parent=None, *, interval=3.0):
        super().__init__(parent)
        if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
            raise ValueError('Invalid index interval')
        self.store = store
        self.interval = float(interval)
        self._lock = threading.Lock()
        self._directory = self._path(directory)
        self._generation = 0
        self._refresh = 0
        self._stop_event = threading.Event()
        self._wake = threading.Event()

    @staticmethod
    def _path(value):
        return Path(value).expanduser().resolve() if value is not None else None

    @property
    def directory(self):
        with self._lock:
            return self._directory

    def set_directory(self, directory):
        value = self._path(directory)
        with self._lock:
            if value == self._directory:
                return
            self._directory = value
            self._generation += 1
        self._wake.set()

    def request_refresh(self, *_):
        with self._lock:
            self._refresh += 1
        self._wake.set()

    def _snapshot(self):
        with self._lock:
            return self._directory, self._generation, self._refresh

    def _canceled(self, generation):
        if self._stop_event.is_set():
            return True
        with self._lock:
            return self._generation != generation

    def run(self):
        last_generation = None
        last_refresh = None
        last_fingerprint = None
        failure = None
        while not self._stop_event.is_set():
            # Clearing before the snapshot cannot lose a new request: a request
            # after this point changes the snapshot or leaves the wake event set.
            self._wake.clear()
            directory, generation, refresh = self._snapshot()
            try:
                if directory is None:
                    result = dict(indexed=0, unknown=0, changed=0, unchanged=0, files=0, canceled=False)
                    exists = False
                else:
                    result = dict(self.store.scan(directory, stop_requested=lambda: self._canceled(generation)))
                    exists = directory.is_dir()
                if self._canceled(generation):
                    continue
                if result.get('canceled'):
                    self._wake.wait(self.interval)
                    continue
                fingerprint = (exists, result.get('files', 0), result.get('indexed', 0), result.get('unknown', 0))
                initial = generation != last_generation
                explicit = bool(refresh) if last_refresh is None else refresh != last_refresh
                notify = (initial or explicit or failure is not None
                          or bool(result.get('changed')) or fingerprint != last_fingerprint)
                last_generation = generation
                last_refresh = refresh
                last_fingerprint = fingerprint
                failure = None
                if notify:
                    result.update(directory=str(directory) if directory is not None else None,
                                  generation=generation, directory_exists=exists,
                                  initial=initial, refresh_requested=explicit)
                    self.changed.emit(result)
            except Exception as error:
                if self._canceled(generation):
                    continue
                marker = (generation, type(error).__name__)
                if marker != failure:
                    self.failed.emit('티켓 목록을 갱신하지 못했습니다. 저장 폴더와 파일을 확인해 주세요.')
                failure = marker
            self._wake.wait(self.interval)

    def stop(self, timeout=10000):
        self._stop_event.set()
        self._wake.set()
        if self.isRunning() and QThread.currentThread() is not self:
            return self.wait(timeout)
        return True


TicketIndexWorker = IndexWorker
