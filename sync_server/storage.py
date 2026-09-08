"""内容对象按 Vault 分区；测试磁盘适配器不作为生产对象存储。"""

from pathlib import Path
import hashlib
import os
import tempfile
import shutil
from contextlib import contextmanager


class DiskObjects:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes):
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def put_file(self, key: str, path: Path, content_hash: str):
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                with path.open("rb") as source:
                    shutil.copyfileobj(source, stream, 1024 * 1024)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def open(self, key: str):
        with (self.root / key).open("rb") as stream:
            yield stream

    def delete(self, key: str):
        (self.root / key).unlink(missing_ok=True)


class S3Objects:
    def __init__(self, endpoint: str, bucket: str):
        import boto3
        from botocore.config import Config
        self.client = boto3.client("s3", endpoint_url=endpoint,
                                   config=Config(connect_timeout=2, read_timeout=2,
                                                 retries={"max_attempts": 0}))
        self.bucket = bucket

    def put(self, key: str, data: bytes):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                               Metadata={"sha256": hashlib.sha256(data).hexdigest()})

    def get(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with response["Body"] as stream:
            return stream.read()

    def put_file(self, key: str, path: Path, content_hash: str):
        from boto3.s3.transfer import TransferConfig
        with path.open("rb") as stream:
            self.client.upload_fileobj(stream, self.bucket, key,
                ExtraArgs={"Metadata": {"sha256": content_hash}},
                Config=TransferConfig(use_threads=False, max_concurrency=1))

    @contextmanager
    def open(self, key: str):
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with response["Body"] as stream:
            yield stream

    def delete(self, key: str):
        self.client.delete_object(Bucket=self.bucket, Key=key)
