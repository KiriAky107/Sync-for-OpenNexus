"""Bounded worker use, request cancellation, cached failures and recovery."""
import asyncio
from threading import Event

from sync_server.readiness import Readiness


def test_timeout_and_cancel_never_spawn_overlapping_dependency_probes():
    entered, release = Event(), Event()
    calls = []

    def slow():
        calls.append(1)
        entered.set()
        if not release.wait(10):
            raise TimeoutError("fixture was not released")
        raise OSError("late dependency exception must be consumed")

    async def run():
        ready = Readiness(slow, timeout=.01, cache_seconds=0)
        request = asyncio.create_task(ready.check())
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            assert ready.running is not None and not ready.running.done()
            assert await asyncio.gather(*(ready.check() for _ in range(20))) == [False] * 20
            assert len(calls) == 1
        finally:
            release.set()
            await asyncio.gather(ready.running, return_exceptions=True)
        # A completed failed probe does not prevent a new healthy attempt.
        ready.probe = lambda: None
        assert await ready.check() is True
    asyncio.run(run())


def test_success_and_failure_cache_then_refresh():
    calls = []

    def probe():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("controlled dependency outage")

    async def run():
        ready = Readiness(probe, cache_seconds=.02)
        assert await ready.check() is False
        assert await asyncio.gather(*(ready.check() for _ in range(20))) == [False] * 20
        assert len(calls) == 1
        await asyncio.sleep(.03)
        assert await ready.check() is True
        assert await ready.check() is True
        assert len(calls) == 2
    asyncio.run(run())
