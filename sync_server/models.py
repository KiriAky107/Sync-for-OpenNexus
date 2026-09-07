"""协议 v1 DTO；路径在所有平台使用同一套保守规范。"""

import re
import unicodedata
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(DTO):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)
    device_name: str = Field(min_length=1, max_length=120)


class Refresh(DTO):
    refresh_token: str = Field(min_length=32, max_length=256)


class VaultCreate(DTO):
    name: str = Field(min_length=1, max_length=120)


class Upload(DTO):
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0, le=104857600)


def canonical_path(value: str) -> str:
    if value != unicodedata.normalize("NFC", value) or len(value.encode("utf-8")) > 768:
        raise ValueError("路径须为 NFC 且不超过 768 字节")
    for part in value.split("/"):
        if (not part or part in {".", ".."} or part[-1:] in {" ", "."}
                or re.search(r'[<>:"\\|?*\x00-\x1f\x7f]', part)
                or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
                or part.casefold() in {".ainote", ".git"}):
            raise ValueError("不可移植或保留路径")
    return value


class Commit(DTO):
    operation_id: str = Field(pattern=r"^[a-zA-Z0-9-]{16,80}$")
    file_id: str = Field(pattern=r"^[a-zA-Z0-9-]{16,80}$")
    base_revision: int = Field(ge=0)
    path: str = Field(min_length=1)
    operation: Literal["put", "delete"]
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    size: int = Field(default=0, ge=0, le=104857600)

    @field_validator("path")
    @classmethod
    def path_valid(cls, value):
        return canonical_path(value)
