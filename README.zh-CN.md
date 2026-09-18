# Sync for OpenNexus

**简体中文** | [English](README.md)

[![版本](https://img.shields.io/badge/version-0.5.2--alpha1-5865f2)](https://github.com/KiriAky107/Sync-for-OpenNexus/releases/tag/v0.5.2-alpha1)
![Python](https://img.shields.io/badge/Python-3.12%2B-3776ab)
![API](https://img.shields.io/badge/API-FastAPI-05998b)
![控制台](https://img.shields.io/badge/console-Vue%203-42b883)
[![许可证](https://img.shields.io/badge/license-MIT-22c55e)](LICENSE)

Sync for OpenNexus 是 OpenNexus 的可选自托管同步服务，管理账户、设备、不可变内容对象、有序文件修订、断点续传、备份及空实例恢复。它不运行桌面 AI Core，也不会直接读取用户本地 Vault。

> **Alpha 状态：**生产部署必须使用 TLS 和访问控制。PostgreSQL 与 S3 兼容对象存储是生产路径；SQLite 和明文 HTTP 仅用于隔离测试。

## 能力与边界

- Access/Refresh 会话和设备撤销。
- 用户级 Vault 隔离、配额和有序修订流。
- 校验偏移、长度、SHA-256 和幂等完成回执的分块上传。
- PostgreSQL 元数据与不可变对象存储分离。
- 供桌面 Outbox/Inbox 客户端使用的基础修订与冲突检测。
- Repeatable-read 备份和只面向空部署的验证恢复。
- 同源 Vue 控制台，用于健康状态、账户、Vault 和设备管理。

服务不执行模型、不索引 Markdown、不安装扩展，也不接收 OpenNexus 模型提供商凭据，更不会复用 Community Token。

## 系统架构

```mermaid
flowchart LR
    Desktop[OpenNexus 桌面客户端] -->|HTTPS Sync v1| Proxy[TLS 反向代理]
    Console[Vue 3 Sync Console] -->|同源 API| Proxy
    Proxy --> API[FastAPI Sync 服务]
    API --> PG[(PostgreSQL 17 元数据)]
    API --> Stage[(受限暂存卷)]
    API --> S3[(S3 兼容对象存储)]
    Init[一次性初始化任务] --> PG
    Init --> S3
    Backup[备份与恢复命令] --> PG
    Backup --> S3
```

`compose.yaml` 默认只绑定 `127.0.0.1:8080`，使用幂等初始化任务、只读服务文件系统、删除 Linux Capabilities，并让长期运行服务使用 Bucket 级凭据而非 MinIO Root 凭据。

## 同步协议流程

```mermaid
sequenceDiagram
    autonumber
    participant Client as 桌面客户端
    participant API as Sync v1 API
    participant DB as PostgreSQL
    participant Obj as 对象存储

    Client->>API: 握手并认证设备
    API->>DB: 创建访问与刷新会话
    Client->>API: 以哈希和长度创建断点上传
    API->>DB: 预留上传与配额
    loop 有界分块
        Client->>API: 在期望偏移上传数据块
        API->>DB: 保存新偏移
    end
    Client->>API: 完成上传
    API->>Obj: 写入不可变对象
    API->>DB: 写入对象记录和完成回执
    Client->>API: 携带基础修订和操作 ID 提交修订
    API->>DB: 校验所有权、路径、序列和幂等性
    alt 基础修订有效
        API->>DB: 追加修订并更新文件 Head
        API-->>Client: 返回新有序序列
    else 冲突
        API-->>Client: 返回当前修订冲突
    end
    Client->>API: 从游标之后拉取变化
    API-->>Client: 返回有序修订与对象哈希
    Client->>API: 下载所需不可变对象
```

## 数据库关系

```mermaid
erDiagram
    USERS ||--o{ DEVICES : 拥有
    DEVICES ||--o{ SESSIONS : 认证
    USERS ||--o{ VAULTS : 拥有
    VAULTS ||--o{ UPLOADS : 暂存
    DEVICES ||--o{ UPLOADS : 创建
    VAULTS ||--o{ OBJECTS : 保存
    VAULTS ||--o{ REVISIONS : 追加
    DEVICES ||--o{ REVISIONS : 提交
    VAULTS ||--o{ FILES : 跟踪
    UPLOADS ||--o| UPLOAD_RECEIPTS : 完成

    USERS { string id PK string username UK string password_hash }
    DEVICES { string id PK string user_id FK string name bool revoked }
    SESSIONS { string token PK string refresh UK string device_id FK int expires int refresh_expires }
    VAULTS { string id PK string user_id FK string name int sequence int quota int used }
    UPLOADS { string id PK string vault_id FK string device_id FK string hash int size int offset_bytes int expires }
    OBJECTS { string vault_id PK, FK string hash PK int size int created }
    REVISIONS { string vault_id PK, FK int sequence PK string file_id int base_revision string path string operation string hash string device_id FK string operation_id UK }
    FILES { string vault_id PK, FK string file_id PK int sequence string path_key bool deleted }
    UPLOAD_RECEIPTS { string id PK string vault_id FK string device_id FK string hash int completed }
```

Schema 还包含 `schema_version`、`login_limits` 和 `bootstrap_state`。对象内容位于 S3，PostgreSQL 是所有权、修订顺序、配额、回执和对象目录的权威来源。

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `sync_server/` | API、协议、数据库、存储、就绪检查、备份与恢复 |
| `console/` | Vue 3 + TypeScript 管理控制台 |
| `tests/` | 协议、生产存储、就绪状态、控制台和基准测试 |
| `tools/` | 有界上传与传输探针 |
| `compose.yaml` | PostgreSQL、MinIO、初始化任务和加固服务 |
| `compose.test.yaml` | 显式隔离测试覆盖配置 |

## 快速部署

```powershell
Copy-Item .env.example .env
# 为所有空白密钥生成彼此独立的值并编辑 .env
docker compose up -d --build
docker compose ps
docker compose logs sync
```

全新数据库会生成临时 `admin`，并向 Sync 容器日志写入 `SYNC_BOOTSTRAP_CREDENTIALS`。首次登录后必须立即修改用户名和密码；在固定凭据前，每次重启都会轮换临时密码并撤销旧会话。

| 环境变量 | 用途 |
| --- | --- |
| `POSTGRES_PASSWORD` | PostgreSQL 容器密码 |
| `SYNC_DATABASE_URL` | 密码经过 URL 编码的 PostgreSQL SQLAlchemy URL |
| `MINIO_ROOT_USER`、`MINIO_ROOT_PASSWORD` | 仅供初始化任务管理对象存储 |
| `SYNC_ACCESS_KEY_ID`、`SYNC_SECRET_ACCESS_KEY` | Bucket 级运行身份 |
| `SYNC_S3_BUCKET` | 已有或初始化的对象 Bucket |
| `SYNC_BIND_ADDRESS`、`SYNC_PORT` | 宿主监听地址与端口 |

不得复用 MinIO Root 凭据作为运行时凭据，升级时必须保持 Bucket 名称一致。

## 健康与运维

- `/health` 报告进程健康。
- `/ready` 检查数据库 Schema、暂存目录写入及对象存储探针。
- `/` 与 `/console/` 提供同源管理控制台。
- 生产流量必须在反向代理终止 TLS；可参考 `Caddyfile.example`。
- `compose.test.yaml` 直接暴露端口的方式仅用于已授权隔离演示。

## 备份与空实例恢复

```powershell
python -m sync_server backup --directory D:/OpenNexus-backups/latest --io-workers 8
python -m sync_server restore --directory D:/OpenNexus-backups/latest --io-workers 8
```

```mermaid
flowchart LR
    A[启动 repeatable-read 事务] --> B[导出 Schema v1 数据]
    B --> C[生成不可变对象清单]
    C --> D{存在未完成上传或缺失对象？}
    D -- 是 --> X[失败且不发布残缺备份]
    D -- 否 --> E[流式下载并校验长度与 SHA-256]
    E --> F[写入完成的备份元数据]
    F --> G{目标数据库与 Bucket 为空？}
    G -- 否 --> Y[拒绝恢复]
    G -- 是 --> H[校验备份完整性与时间策略]
    H --> I[上传并回读全部对象]
    I --> J[在单个 PostgreSQL 事务导入数据]
    J --> K[启动服务并检查 ready]
```

备份包含认证和会话哈希，必须使用受限 ACL、加密存储、保留规则和异机副本保护，并定期在独立空实例演练恢复。

## 开发与测试

```powershell
uv sync --frozen
uv run pytest

cd console
corepack enable
corepack prepare pnpm@10.28.0 --activate
pnpm install --frozen-lockfile
pnpm type-check
pnpm build
```

测试只能使用临时数据库、对象目录和暂存目录，不得读取 OpenNexus 用户 Vault 或真实部署凭据。

## 安全与社区

- 不得提交 `.env`、Token、密码、数据库、对象内容、备份或用户 Vault。
- 限制数据库和对象存储网络，启用监控，并及时撤销遗失设备。
- 路径规范化、操作 ID、基础修订、配额、内容哈希和账户边界均属于安全控制。
- 漏洞按照 [SECURITY.md](SECURITY.md) 私下报告。
- 贡献遵循[贡献指南](CONTRIBUTING.md)和[社区行为准则](CODE_OF_CONDUCT.md)。
- 使用仓库 Issue 表单和 PR 模板，并对基础设施与账户信息脱敏。

相关仓库：[OpenNexus](https://github.com/KiriAky107/OpenNexus) 与 [Community for OpenNexus](https://github.com/KiriAky107/Community-for-OpenNexus)。

## 许可证

本项目采用 [MIT License](LICENSE)，第三方组件继续适用各自的许可证与声明。
