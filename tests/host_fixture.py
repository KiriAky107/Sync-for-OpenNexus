"""Single-worker localhost fixture for real Rust HTTP interoperability, never deployment."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import threading
import uvicorn
from sync_server.app import create_app
from sync_server.database import Database
from sync_server.storage import DiskObjects


def main():
    root = Path(sys.argv[1])
    if not root.is_dir() or not (root / '.opennexus-test').is_file():
        raise SystemExit('ISOLATED_TEST_ROOT_REQUIRED')
    database = Database('sqlite:///' + str(root / 'sync.sqlite3'))
    app = create_app(database, DiskObjects(root / 'objects'), root / 'staging')
    database.add_user('rust-fixture', 'controlled-fixture-password')
    @app.middleware('http')
    async def interrupt_upload(request, call_next):
        response = await call_next(request)
        if (root / 'interrupt-upload').exists() and request.method == 'PUT' and '/uploads/' in request.url.path and response.status_code == 200:
            offset = int(request.query_params.get('offset', '0')) + int(request.headers.get('content-length', '0'))
            if offset and offset % (10 * 1024 * 1024) == 0:
                marker = root / 'upload-boundary'
                marker.write_text(str(offset), encoding='ascii')
                for _ in range(600):
                    if not marker.exists(): break
                    await asyncio.sleep(.05)
        if (root / 'interrupt-revision').exists() and request.method == 'POST' and request.url.path.endswith('/revisions') and response.status_code == 200:
            marker = root / 'revision-committed'
            marker.write_text('committed', encoding='ascii')
            for _ in range(600):
                if not (root / 'interrupt-revision').exists(): break
                await asyncio.sleep(.05)
        return response
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=1))
    def parent():
        sys.stdin.buffer.read()
        server.should_exit = True
    threading.Thread(target=parent, daemon=True).start()
    async def run():
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if task.done(): await task; raise RuntimeError('FIXTURE_START_FAILED')
            await asyncio.sleep(.01)
        print(json.dumps({'port': sock.getsockname()[1]}), flush=True)
        await task
    try: asyncio.run(run())
    finally: sock.close(); database.engine.dispose()


if __name__ == '__main__': main()
