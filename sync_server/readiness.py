"""Bound readiness work even when a synchronous dependency ignores its timeout."""
import asyncio
import time


class Readiness:
    def __init__(self, probe, *, timeout=3, cache_seconds=5):
        self.probe = probe
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.lock = asyncio.Lock()
        self.running = None
        self.until = 0.0
        self.ok = False

    @staticmethod
    def consume(task):
        # A request can time out or disconnect before the synchronous probe ends.
        # Retrieve late exceptions without logging dependency messages/secrets.
        if not task.cancelled():
            task.exception()

    async def check(self):
        async with self.lock:
            if time.monotonic() < self.until:
                return self.ok
            if self.running is None or self.running.done():
                self.running = asyncio.create_task(asyncio.to_thread(self.probe))
                self.running.add_done_callback(self.consume)
            try:
                # Cancelling a to_thread await does not stop its OS thread. Keep
                # the task alive so subsequent requests reuse the same probe.
                await asyncio.wait_for(asyncio.shield(self.running), self.timeout)
                self.ok = True
            except Exception:
                self.ok = False
            self.until = time.monotonic() + self.cache_seconds
            return self.ok
