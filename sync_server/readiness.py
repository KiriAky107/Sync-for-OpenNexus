"""即使同步依赖项忽略其超时，绑定准备工作也会起作用。"""
import asyncio
import time


class DependencyFailure(RuntimeError):
    def __init__(self, code):
        self.code = code if code in {'DATABASE_UNAVAILABLE', 'SCHEMA_INCOMPATIBLE', 'STAGING_INTEGRITY', 'STAGING_UNAVAILABLE', 'OBJECT_STORAGE_INTEGRITY', 'OBJECT_STORAGE_UNAVAILABLE'} else 'DEPENDENCY_UNAVAILABLE'
        super().__init__(self.code)


class Readiness:
    def __init__(self, probe, *, timeout=3, cache_seconds=5):
        self.probe = probe
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.lock = asyncio.Lock()
        self.running = None
        self.until = 0.0
        self.ok = False
        self.code = 'NOT_CHECKED'
        self.started_at = None
        self.checked_at = None

    @staticmethod
    def consume(task):
        # 在同步探测结束之前，请求可能会超时或断开连接。检索晚期异常而不记录依赖项消息/秘密。
        if not task.cancelled():
            task.exception()

    async def check(self):
        return (await self.snapshot())['ready']

    async def snapshot(self):
        async with self.lock:
            if time.monotonic() < self.until:
                return self.result(cached=True)
            if self.running is None or self.running.done():
                self.started_at = time.time_ns()//1_000_000
                self.running = asyncio.create_task(asyncio.to_thread(self.probe))
                self.running.add_done_callback(self.consume)
            try:
                # 取消 to_thread 等待不会停止其 OS 线程。保持任务处于活动状态，以便后续请求重用相同的探测器。
                await asyncio.wait_for(asyncio.shield(self.running), self.timeout)
                self.ok = True
                self.code = 'READY'
            except TimeoutError:
                self.ok = False
                self.code = 'DEPENDENCY_TIMEOUT'
            except Exception as failure:
                self.ok = False
                self.code = failure.code if isinstance(failure,DependencyFailure) else 'DEPENDENCY_UNAVAILABLE'
            self.checked_at = time.time_ns()//1_000_000
            self.until = time.monotonic() + self.cache_seconds
            return self.result(cached=False)

    def result(self, *, cached):
        return {'ready':self.ok,'code':self.code,'cached':cached,
                'started_at':self.started_at,'checked_at':self.checked_at,
                'probe_pending':self.running is not None and not self.running.done()}
