"""即使同步依赖项忽略其超时，绑定准备工作也会起作用。"""
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
        # 在同步探测结束之前，请求可能会超时或断开连接。检索晚期异常而不记录依赖项消息/秘密。
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
                # 取消 to_thread 等待不会停止其 OS 线程。保持任务处于活动状态，以便后续请求重用相同的探测器。
                await asyncio.wait_for(asyncio.shield(self.running), self.timeout)
                self.ok = True
            except Exception:
                self.ok = False
            self.until = time.monotonic() + self.cache_seconds
            return self.ok
