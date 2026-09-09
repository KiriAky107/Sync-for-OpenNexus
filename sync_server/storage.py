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

    def ensure_bucket(self) -> bool:
        """不在时创建配置的桶；切勿更改现有存储桶。"""
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=self.bucket)
            return False
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code not in {"404", "NoSuchBucket", "NotFound"} and status != 404:
                raise
        self.client.create_bucket(Bucket=self.bucket)
        return True

    def is_empty(self) -> bool:
        response = self.client.list_objects_v2(Bucket=self.bucket, MaxKeys=1)
        return not response.get("Contents")

    def delete_many(self, keys: list[str]) -> None:
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            if batch:
                self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
                )

    def delete_bucket(self) -> None:
        self.client.delete_bucket(Bucket=self.bucket)

    def put(self, key: str, data: bytes):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                               Metadata={"sha256": hashlib.sha256(data).hexdigest()})

    def get(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with response["Body"] as stream:
            return stream.read()

    def put_file(self, key: str, path: Path, content_hash: str):
        with path.open("rb") as stream:
            # 对象的上限为 100 MiB，远低于 S3 的 5 GiB 单 PUT 限制。直接流请求有一个显式的连接生命周期；每次完成构建一个传输管理器可以在重复的多工作线程使用下保留池化的 MinIO 连接。
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=stream,
                ContentLength=path.stat().st_size,
                Metadata={"sha256": content_hash},
            )

    @contextmanager
    def open(self, key: str):
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with response["Body"] as stream:
            yield stream

    def delete(self, key: str):
        self.client.delete_object(Bucket=self.bucket, Key=key)
