"""内容对象按 Vault 分区；测试磁盘适配器不作为生产对象存储。"""

from pathlib import Path
import hashlib
import os
import tempfile


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

    def delete(self, key: str):
        (self.root / key).unlink(missing_ok=True)


class S3Objects:
    def __init__(self, endpoint: str, bucket: str):
        import boto3
        self.client = boto3.client("s3", endpoint_url=endpoint)
        self.bucket = bucket

    def put(self, key: str, data: bytes):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                               Metadata={"sha256": hashlib.sha256(data).hexdigest()})

    def get(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with response["Body"] as stream:
            return stream.read()

    def delete(self, key: str):
        self.client.delete_object(Bucket=self.bucket, Key=key)
