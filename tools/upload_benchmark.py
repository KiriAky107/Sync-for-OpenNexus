"""四个并发上传/下载探针；仅针对一次性测试服务运行。两个现有测试帐户的凭据 JSON 为 [{"username": "...", "password": "..."}, ...]。切勿将凭证、令牌或响应正文写入报告。这是一个转移探针，不是 S-09 完成或 RSS 测量的声明。"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

import httpx

CHUNK = 1024 * 1024


class ProbeFailure(Exception):
    pass


async def checked(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    if response.status_code != 200:
        raise ProbeFailure(f"HTTP_{response.status_code}")
    return response.json()


def block(index, offset, length):
    # 每个流和偏移量具有独特的、可重复的内容，没有整个文件缓冲区。
    seed = hashlib.sha256(f"20260908:{index}:{offset}".encode()).digest()
    return (seed * ((length + len(seed) - 1) // len(seed)))[:length]


def expected_hash(index, size):
    checksum = hashlib.sha256()
    for offset in range(0, size, CHUNK):
        checksum.update(block(index, offset, min(CHUNK, size - offset)))
    return checksum.hexdigest()


async def probe(base_url, credentials, *, size=100 * CHUNK):
    if len(credentials) != 2 or credentials[0]["username"] == credentials[1]["username"]:
        raise ValueError("TWO_DISTINCT_TEST_ACCOUNTS_REQUIRED")
    if not 1 <= size <= 100 * CHUNK:
        raise ValueError("SIZE_OUT_OF_RANGE")
    run_id = uuid.uuid4().hex
    report = {"run_id": run_id, "seed": 20260908, "bytes_per_upload": size,
              "concurrency": 4, "accounts": 2, "vaults_per_account": 2,
              "result": "FAILED", "acceptance": "NOT_ASSESSED",
              "service_rss_bytes": None,
              "limitations": ["No server process/container RSS collector attached",
                  "Not the 30-minute load, 10000-note or impaired-network benchmark",
                  "Creates four test Vaults and retains uploaded data for inspection"],
              "transfers": []}
    timeout = httpx.Timeout(60, connect=10)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout,
                                follow_redirects=False, trust_env=False) as admin:
        sessions = []
        for account in credentials:
            session = await checked(admin, "POST", "/sync/v1/auth/sessions", json={
                **account, "device_name": "upload-benchmark-" + run_id})
            sessions.append({"Authorization": "Bearer " + session["access_token"]})
        jobs = []
        for index in range(4):
            auth = sessions[index // 2]
            vault = await checked(admin, "POST", "/sync/v1/vaults", headers=auth,
                                  json={"name": f"benchmark-{run_id}-{index}"})
            jobs.append((index, auth, "/sync/v1/vaults/" + vault["vault_id"]))
        hashes = [expected_hash(index, size) for index in range(4)]
        gate = asyncio.Event()
        ready = asyncio.Queue()

        async def transfer(index, auth, base):
            async with httpx.AsyncClient(base_url=base_url, headers=auth, timeout=timeout,
                                        follow_redirects=False, trust_env=False) as client:
                sha = hashes[index]
                info = await checked(client, "POST", base + "/uploads",
                                     json={"content_hash": sha, "size": size})
                if info["complete"] or info["offset"] != 0:
                    raise ProbeFailure("NEW_VAULT_UPLOAD_NOT_EMPTY")
                upload = base + "/uploads/" + info["upload_id"]
                await ready.put(index)
                await gate.wait()
                start = time.perf_counter()
                for offset in range(0, size, CHUNK):
                    data = block(index, offset, min(CHUNK, size - offset))
                    result = await checked(client, "PUT", upload, params={"offset": offset}, content=data)
                    if result["offset"] != offset + len(data):
                        raise ProbeFailure("OFFSET_MISMATCH")
                receipt = await checked(client, "POST", upload + "/complete")
                if receipt != {"complete": True, "content_hash": sha}:
                    raise ProbeFailure("COMPLETE_MISMATCH")
                if await checked(client, "POST", upload + "/complete") != receipt:
                    raise ProbeFailure("COMPLETE_REPLAY_MISMATCH")
                uploaded = time.perf_counter()
                revision = {"operation_id": uuid.uuid4().hex, "file_id": uuid.uuid4().hex,
                            "base_revision": 0, "path": "attachments/benchmark.bin",
                            "operation": "put", "content_hash": sha, "size": size}
                committed = await checked(client, "POST", base + "/revisions", json=revision)
                if committed["sequence"] != 1:
                    raise ProbeFailure("REVISION_SEQUENCE_MISMATCH")
                if await checked(client, "POST", base + "/revisions", json=revision) != committed:
                    raise ProbeFailure("REVISION_REPLAY_MISMATCH")
                checksum, received = hashlib.sha256(), 0
                async with client.stream("GET", base + "/objects/" + sha) as response:
                    if response.status_code != 200:
                        raise ProbeFailure(f"DOWNLOAD_HTTP_{response.status_code}")
                    async for data in response.aiter_bytes(CHUNK):
                        received += len(data)
                        if received > size:
                            raise ProbeFailure("DOWNLOAD_SIZE_EXCEEDED")
                        checksum.update(data)
                if received != size or checksum.hexdigest() != sha:
                    raise ProbeFailure("DOWNLOAD_INTEGRITY")
                # 另一个帐户必须无法读取此对象的字节。
                denied = await admin.get(base + "/objects/" + sha, headers=sessions[1 - index // 2])
                if denied.status_code not in (403, 404):
                    raise ProbeFailure("ACCOUNT_ISOLATION_FAILED")
                return {"index": index, "vault_id": base.rsplit("/", 1)[1],
                        "sha256": sha, "verified_bytes": received,
                        "upload_seconds": uploaded - start,
                        "total_seconds": time.perf_counter() - start}

        tasks = [asyncio.create_task(transfer(*job)) for job in jobs]
        try:
            # 绑定准备：失败的对等体不能让其他对等体永远等待。
            async def all_ready():
                for _ in jobs:
                    await ready.get()
            readiness = asyncio.create_task(all_ready())
            try:
                finished, _ = await asyncio.wait([readiness, *tasks], timeout=75,
                                                 return_when=asyncio.FIRST_COMPLETED)
                if not finished:
                    raise ProbeFailure("PREPARATION_TIMEOUT")
                for task in finished:
                    task.result()
                if not readiness.done():
                    raise ProbeFailure("PREPARATION_FAILED")
            finally:
                readiness.cancel()
                await asyncio.gather(readiness, return_exceptions=True)
            start = time.perf_counter()
            gate.set()
            report["transfers"] = await asyncio.gather(*tasks)
            report["elapsed_seconds"] = time.perf_counter() - start
            report["result"] = "PASSED"
            return report
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-test-http", action="store_true")
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or
            url.password or url.query or url.fragment or url.path not in ("", "/")):
        parser.error("EXPECTED_SERVICE_ORIGIN")
    if url.scheme == "http" and not args.allow_test_http:
        parser.error("HTTP_REQUIRES_ALLOW_TEST_HTTP")
    report = {"result": "FAILED", "acceptance": "NOT_ASSESSED"}
    try:
        credentials = json.loads(args.credentials.read_text(encoding="utf-8"))
        report = asyncio.run(asyncio.wait_for(probe(args.url, credentials), timeout=1800))
    except Exception as error:
        # 异常文本可能包含凭据或响应数据；仅记录其类型。
        report["error_type"] = type(error).__name__
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(report["result"])
    return 0 if report["result"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
