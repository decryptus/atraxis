"""Bounded periodic reads; no catch-up queue, overlap or business mutations."""
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time

from atraxis.transport import DEFAULT_CONCURRENCY, HTTPTransport, TransportResult


SCHEDULER_INTERVAL = 0.05
LOG = logging.getLogger(__name__)


class PollingCoordinator:
    def __init__(self, sources, service, *, transport=None):
        self._sources = tuple(sources)
        self._service = service
        self._transport = transport if transport is not None else HTTPTransport()
        self._cancel = threading.Event()
        self._lifecycle = threading.Lock()
        self._thread = None

    def start(self):
        with self._lifecycle:
            if self._cancel.is_set() or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name='atraxis-collector')
            self._thread.start()

    def stop(self):
        with self._lifecycle:
            self._cancel.set()
            thread = self._thread
        # Never hold the lifecycle lock while joining workers.
        if thread is not None:
            thread.join()
        self._service.set_running(False)

    def _fetch(self, source):
        attempted = time.time()
        try:
            return self._transport.fetch(source, cancel=self._cancel)
        except Exception:
            # Isolate unexpected adapter errors without exposing exception text.
            return TransportResult(source.source_id, 'collector_error', attempted,
                                   time.time(), time.monotonic())

    def _run(self):
        due = {source.source_id: 0.0 for source in self._sources}
        active = {}
        self._service.set_running(True)
        try:
            with ThreadPoolExecutor(max_workers=DEFAULT_CONCURRENCY,
                                    thread_name_prefix='atraxis-source') as pool:
                try:
                    while not self._cancel.is_set():
                        for source_id, future in list(active.items()):
                            if future.done():
                                self._service.record(source_id, future.result())
                                source = next(s for s in self._sources if s.source_id == source_id)
                                due[source_id] = time.monotonic() + source.poll_interval
                                del active[source_id]
                        # Oldest due first: slow sources cannot monopolize admission.
                        for source in sorted(self._sources, key=lambda s: due[s.source_id]):
                            if self._cancel.is_set() or len(active) >= DEFAULT_CONCURRENCY:
                                break
                            if source.source_id not in active and due[source.source_id] <= time.monotonic():
                                active[source.source_id] = pool.submit(self._fetch, source)
                        self._cancel.wait(SCHEDULER_INTERVAL)
                finally:
                    # Cancel before the executor joins, including scheduler errors.
                    self._cancel.set()
                    self._service.set_running(False)
        except Exception:
            LOG.error('collector_stopped_unexpectedly')
        finally:
            self._service.set_running(False)
