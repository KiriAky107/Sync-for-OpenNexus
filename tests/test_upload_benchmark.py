"""真实的 localhost HTTP 测试框架；使用 SQLite/DiskObjects，并非生产拓扑。"""
import asyncio
import json
import socket

import pytest
import uvicorn

from sync_server.app import create_app
from sync_server.database import Database
from sync_server.storage import DiskObjects
from tools.upload_benchmark import probe


@pytest.mark.parametrize("size", [1048576 + 17, 100 * 1048576])
def test_four_concurrent_uploads_over_real_http(tmp_path, size):
    database = Database("sqlite:///" + str(tmp_path / "sync.db"))
    app = create_app(database, DiskObjects(tmp_path / "objects"), tmp_path / "staging")
    credentials = [{"username": username, "password": "controlled-fixture-password"}
                   for username in ("benchmark-one", "benchmark-two")]
    for account in credentials:
        database.add_user(**account)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False,
                                           timeout_graceful_shutdown=2))

    async def run():
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async def ready():
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("SERVER_START_FAILED")
                    await asyncio.sleep(.01)
            await asyncio.wait_for(ready(), 10)
            report = await asyncio.wait_for(probe(f"http://127.0.0.1:{sock.getsockname()[1]}",
                                                   credentials, size=size), 180)
            assert report["result"] == "PASSED"
            assert report["service_rss_bytes"] is None
            assert report["acceptance"] == "NOT_ASSESSED"
            assert len(report["transfers"]) == 4
            assert len({item["sha256"] for item in report["transfers"]}) == 4
            assert all(item["verified_bytes"] == size for item in report["transfers"])
            print(json.dumps(report))
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)
    try:
        asyncio.run(run())
    finally:
        sock.close()
        database.engine.dispose()
