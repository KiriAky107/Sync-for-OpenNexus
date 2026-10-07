<div align="center">

  <img src=".github/assets/opennexus-logo.svg" alt="OpenNexus Logo" width="100" height="100" />

  <h1>Sync for OpenNexus</h1>

  <p><strong>自托管的知识库同步服务</strong></p>

  <p>在自己的基础设施上同步 OpenNexus 知识库，保留文件修订，并在需要时恢复数据。</p>

  <p>
    <a href="README.md">English</a> • <a href="#快速开始">快速开始</a> • <a href="#核心亮点">核心亮点</a> • <a href="#系统架构">系统架构</a> • <a href="#本地开发">本地开发</a> • <a href="https://github.com/KiriAky107/Sync-for-OpenNexus/releases">发布日志</a>
  </p>

  <p>
    <a href="https://github.com/KiriAky107/Sync-for-OpenNexus/releases/tag/v0.6.0"><img src="https://img.shields.io/badge/Version-0.6.0-5865f2?style=flat-square" alt="版本" /></a> <a href="https://github.com/KiriAky107/Sync-for-OpenNexus/actions/workflows/ci.yml"><img src="https://github.com/KiriAky107/Sync-for-OpenNexus/actions/workflows/ci.yml/badge.svg" alt="CI" /></a> <img src="https://img.shields.io/badge/Python-3.12%2B-3776ab?style=flat-square" alt="Python 3.12+" /> <img src="https://img.shields.io/badge/API-FastAPI-05998b?style=flat-square" alt="FastAPI" /> <img src="https://img.shields.io/badge/Console-Vue_3-42b883?style=flat-square" alt="Vue 3" /> <img src="https://img.shields.io/badge/Metadata-PostgreSQL-4169e1?style=flat-square" alt="PostgreSQL" /> <img src="https://img.shields.io/badge/Objects-S3_compatible-f97316?style=flat-square" alt="S3-compatible storage" /> <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-22c55e?style=flat-square" alt="MIT License" /></a>
  </p>

</div>

---

当前版本： [v0.6.0](https://github.com/KiriAky107/Sync-for-OpenNexus/releases/tag/v0.6.0)。

当前开发新增本账户存储账目、上传管理与经过审核的对象回收。用量分别显示当前唯一对象、仅历史引用的对象、未引用对象和有效上传预留，并提供服务器确认时间。历史仍无限保留。

## 0.6.0 更新

- 固定实验源文件、输入及成果的同步类型与能力声明，配套 OpenNexus 0.6.0。
- 提供设备与用量查询、可信进度及冲突回执，保持分块续传、摘要校验和幂等完成。
- 管理控制台支持账户隔离、就绪检查和持久操作记录；CLI 可核对并恢复未知操作结果。
- PostgreSQL 与 S3 部署提供完整备份校验和空目标恢复，逐字核对数据库、对象和设备撤销状态。
- GitHub CI 验证协议、控制台及部署包，发布固定源码、部署归档和 SHA-256 清单。

## 核心亮点

- **设备与会话**：桌面客户端使用 Access/Refresh 会话接入，可在控制台撤销遗失设备。
- **可移植知识库文件**：笔记、附件、实验源码与输入通过稳定文件身份和规范相对路径传输。
- **断点续传**：从已确认的分块偏移继续上传，完成后先校验长度和 SHA-256，再提交文件修订。
- **保留历史**：保存有序修订和不可变对象，以基础修订检查发现并发编辑，避免静默替换内容。
- **用量与隔离**：各账户的知识库分别管理，配额按对象账目和上传预留空间执行。
- **恢复与运维**：校验备份、恢复到空实例，并通过 CLI 或同源 Vue 控制台查看持久操作记录。

## 快速开始

使用 TLS 反向代理、PostgreSQL 元数据和 S3 兼容对象存储部署。Compose 默认将 API 绑定到 `127.0.0.1:8080`。

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

通过反向代理访问 `/console/`，登录后固定初始凭据并创建远端知识库。SQLite 和明文 HTTP 测试实例用于隔离验收。

## 核心工作流

### 1. 连接并同步知识库

1. 登录控制台并创建远端知识库，配置其他设备时使用同一知识库身份。
2. 在 OpenNexus 中配置 Sync 服务地址、账户和远端知识库。每台设备使用独立会话，模型提供商凭据与 Sync 凭据分别管理。
3. 执行同步、查看进度，并在选择保留内容前审核冲突。文件改名保留稳定身份，服务不会执行上传的文件。

### 2. 审核并恢复文件

1. 在控制台打开**文件与历史**，选择知识库与文件。可按相对路径搜索或包含删除记录；分页列表在刷新前保持同一快照。
2. 选择修订，查看时间、来源设备、路径与恢复来源。旧记录没有可靠时间时保持未知。对比两侧完整文本预览和有界逐行差异，或查看安全图片预览；不支持预览的文件仍提供原始大小和摘要供核对。
3. 核对目标路径并确认当前内容与历史来源。已改名文件默认沿用现名；删除文件的原路径被占用时，选择其他位置。恢复会沿用文件身份，创建新修订。
4. 当前修订变化后，刷新并重新审核。回执丢失时先点击**核对恢复结果**；未找到完成回执后才提供重试，并保留原操作 ID，后续编辑不会因此被覆盖。

### 3. 管理设备访问

在控制台设备列表中撤销选定会话，该设备的下一次认证请求即会失败。知识库文件与历史修订仍供其他授权设备访问。

### 4. 查看用量并取消上传

打开**用量与上传**并选择知识库，按服务器确认时间核对当前对象、保留历史、未引用对象和上传预留。从本账户各设备的列表选择上传，查看偏移与期限，核对编号后再确认取消。回执丢失时先点击**查询原操作结果**；服务器确认上传仍未完成后才提供重试，且保留原上传 ID。先完成的对象会保留。过期上传不再预留配额，由后台暂存清理移除，历史对象仍然可用。

### 5. 恢复前校验备份

先创建备份、运行 `verify-backup` 并核对保存的回执。仅向空数据库和空对象桶恢复，之后检查 `/ready`，再连接测试设备。命令和未知结果处理见[备份与恢复](#备份与恢复)。

### 6. 回收前审核未引用对象

升级全部服务工作进程后，有数据库和对象存储访问权限的管理员可以创建新备份，再执行以下命令。将 `VAULT_ID` 和 `PLAN_ID` 替换为实际编号。创建备份前，先完成或取消未完成上传。

```powershell
python -m sync_server backup --directory D:/OpenNexus-backups/before-gc
python -m sync_server gc-preview --directory D:/OpenNexus-backups/before-gc --vault-id VAULT_ID --grace-hours 168 --limit 20
python -m sync_server gc-status --plan-id PLAN_ID
python -m sync_server gc-apply --directory D:/OpenNexus-backups/before-gc --plan-id PLAN_ID --confirm-plan PLAN_ID
```

确认前核对固定候选的哈希、大小、保护计数和总量。默认宽限期为七天，最短为 24 小时。所有修订都受保护，包括已删除文件的历史；未完成上传和近期完成回执同样阻止回收。候选不在已校验备份中时会被排除。预览一小时后过期，知识库或候选引用变化后需重新预览。

`gc-status` 只读核对原计划。计划仍在运行且存在待核对对象时，应先检查原进程，再使用同一编号和备份继续 `gc-apply`。结果未知的对象不能被新修订引用，原账目保持到删除确认；逐对象结果区分已回收、受保护和待核对。保留备份以便恢复到空实例。回收不裁剪修订游标，也不会自动执行。

旧完成上传的未引用对象被回收后，结果变为 `reclaimed`，再次完成返回 `OBJECT_RECLAIMED`，需要重新上传后再提交修订。已引用对象及其完成重试仍可用。控制台会单独提示待核对的回收对象。

### 7. 开通账户并查看服务诊断

先固定初始凭据，用 `operator-id` 查询管理员的稳定账户 ID，再按[备份与恢复](#备份与恢复)配置 `SYNC_OPERATOR_USER_IDS`。重新登录后可看到**我的知识库 / 服务管理**入口。账户叫 `admin` 不会自动获得管理角色；设备仍受会话过期和撤销检查约束。

1. 在**服务管理**中选择**开通账户**，填写账户和初始密码，设置默认配额，再审核并确认。密码输入框在请求前清空；控制台不持久保存密码或令牌。
2. 选择账户，分页查看知识库和设备。调整单库配额或撤销设备前，核对固定的账户与资源。默认配额修改仅影响以后创建的知识库。
3. 回执中断时选择**查询原管理结果**。只有服务器确认未找到回执后，才按原操作编号重试；核对期间保持原审核动作。
4. 在**服务诊断**查看依赖故障分类、账目计数和清理结果。探测时间与缓存标记区分当前读取与缓存探测；部署指引下方显示备份与恢复回执。

控制台使用相同的 API 路由：

| 操作 | API |
| --- | --- |
| 查看角色与分页账户 | `GET /sync/v1/admin/access`、`GET /sync/v1/admin/accounts` |
| 创建账户 | `POST /sync/v1/admin/accounts`，携带 `operation_id`、`username`、`password`、`default_quota` |
| 查看账户的分页知识库、设备和汇总 | `GET /sync/v1/admin/accounts/{user_id}` |
| 设置未来知识库的默认配额 | `PUT .../accounts/{user_id}/policy`，携带 `operation_id`、`expected_revision`、`quota` |
| 调整单个知识库配额 | `PUT .../accounts/{user_id}/vaults/{vault_id}/quota`，携带 `operation_id`、`expected_quota`、`quota` |
| 撤销指定账户的设备 | `POST .../accounts/{user_id}/devices/{device_id}/revoke`，携带 `operation_id` |
| 核对未知写入结果 | `GET /sync/v1/admin/operations/{operation_id}` |
| 读取分类诊断 | `GET /sync/v1/admin/diagnostics` |

每个审核后的动作使用新的 32 位小写十六进制操作 ID。回执丢失时先读取原 ID；已完成结果不会覆盖后来的密码或配额设置。创建结果不返回密码，回执不保存明文密码。默认配额仅应用于新知识库；既有账户在明确配置前沿用部署默认值。单库配额不能低于已计费对象和有效上传预留的总量。

账户和账户内资源按 `limit` 与稳定 ID 游标分页。账户列表返回 `next_before`；详情返回 `vaults_next_before` 和 `devices_next_before`，后续分别通过 `vault_before` 或 `device_before` 读取。诊断以固定代码区分数据库、暂存、对象存储、完整性和超时，提供探测时间与缓存标记。超时后仍复用尚未结束的探测。普通账户不能读取管理资源。

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

对象服务通过 `Dockerfile.objects` 从 [MinIO 官方安全修复版本](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z) 构建，固定源码提交并检查归档 SHA-256，不依赖已无法拉取的公共容器镜像，数据仍保存在既有 `/data` 卷。GitHub CI 使用同一构建文件，验证备份、数据库全部行的逐字恢复、对象哈希、恢复后的设备撤销及非空目标拒绝；上传产物只包含脱敏结果。

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
    USERS ||--o{ DEVICES : owns
    DEVICES ||--o{ SESSIONS : authenticates
    USERS ||--o{ VAULTS : owns
    VAULTS ||--o{ UPLOADS : stages
    DEVICES ||--o{ UPLOADS : creates
    VAULTS ||--o{ OBJECTS : stores
    VAULTS ||--o{ REVISIONS : appends
    DEVICES ||--o{ REVISIONS : submits
    VAULTS ||--o{ FILES : tracks
    UPLOADS ||--o| UPLOAD_RECEIPTS : completes_as

    USERS {
        string id PK
        string username UK
        string password_hash
    }
    DEVICES {
        string id PK
        string user_id FK
        string name
        bool revoked
    }
    SESSIONS {
        string token PK
        string refresh UK
        string device_id FK
        int expires
        int refresh_expires
    }
    VAULTS {
        string id PK
        string user_id FK
        string name
        int sequence
        int quota
        int used
    }
    UPLOADS {
        string id PK
        string vault_id FK
        string device_id FK
        string hash
        int size
        int offset_bytes
        int expires
    }
    OBJECTS {
        string vault_id PK, FK
        string hash PK
        int size
        int created
    }
    REVISIONS {
        string vault_id PK, FK
        int sequence PK
        string file_id
        int base_revision
        string path
        string operation
        string hash
        string device_id FK
        string operation_id UK
    }
    FILES {
        string vault_id PK, FK
        string file_id PK
        int sequence
        string path_key
        bool deleted
    }
    UPLOAD_RECEIPTS {
        string id PK
        string vault_id FK
        string device_id FK
        string hash
        int completed
    }
```

Schema 还包含 `schema_version`、`login_limits`、`bootstrap_state` 和 `revision_annotations`。附加记录保存已知修订时间与恢复来源；旧修订缺少这些证据时，时间保持未知。对象内容位于 S3，PostgreSQL 是所有权、修订顺序、配额、回执和对象目录的权威来源。

### 不透明文件内容与实验文件兼容

握手声明 `encryption: transport-only`。部署时使用 HTTPS / TLS；服务仍可读取对象内容及文件路径，传输加密不是端到端加密。新增的 `features` 声明标识已有的 SHA-256 对象、稳定文件身份、规范路径和已确认偏移续传契约。同步不会执行接收的文件；只使用原有 v1 字段的客户端可以继续忽略此声明。

Sync API v1 将对象内容作为不透明字节传输，校验规范相对路径、内容摘要、对象大小和修订所有权。历史预览另外解码大小受限的文本和图片，服务始终不执行文件。OpenNexus 桌面客户端可沿用现有修订与对象协议同步知识库根目录 `experiments/` 下的 `.py` 源码以及 `.json`、`.csv` 输入数据，无需新增 API 版本。桌面端负责限制哪些本地文件类型进入同步；不认识这些扩展名的旧客户端会忽略它们，不会运行文件。

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `sync_server/` | API、协议、数据库、存储、就绪检查、备份与恢复 |
| `console/` | Vue 3 + TypeScript 管理控制台 |
| `tests/` | 协议、生产存储、就绪状态、控制台和基准测试 |
| `tools/` | 有界上传与传输探针 |
| `compose.yaml` | PostgreSQL、MinIO、初始化任务和加固服务 |
| `compose.test.yaml` | 显式隔离测试覆盖配置 |

## 生态项目

| 仓库 | 职责 |
| --- | --- |
| [OpenNexus](https://github.com/KiriAky107/OpenNexus) | 本地知识库编辑、AI 工作流与经过审核的扩展安装 |
| [Sync for OpenNexus](https://github.com/KiriAky107/Sync-for-OpenNexus) | 可选的自托管知识库同步与恢复 |
| [Community for OpenNexus](https://github.com/KiriAky107/Community-for-OpenNexus) | 独立签名扩展目录与发布审核 |

服务按需启用、分别部署。Sync 使用 `/sync/v1`，Community 使用 `/catalog/v1`；产品版本和协议版本分别维护。

## 健康与运维

- `/health` 报告进程健康。
- `/ready` 检查数据库 Schema、暂存目录写入及对象存储探针。
- `/` 与 `/console/` 提供同源管理控制台。
- 生产流量必须在反向代理终止 TLS；可参考 `Caddyfile.example`。
- `compose.test.yaml` 直接暴露端口的方式仅用于已授权隔离演示。

## 备份与恢复

```powershell
python -m sync_server backup --directory D:/OpenNexus-backups/latest --io-workers 8
python -m sync_server verify-backup --directory D:/OpenNexus-backups/latest --io-workers 8
python -m sync_server restore --directory D:/OpenNexus-backups/latest --io-workers 8
python -m sync_server operation-records --limit 20
```

`verify-backup` 校验数据库快照、每个对象及恢复使用的时间策略，无需连接 PostgreSQL 或 S3。`--max-age-hours` 默认 24 小时。备份、校验和恢复会先保存操作意图，再持久保存结果；记录路径由 `SYNC_OPERATIONS_PATH` 指定。容器部署使用独立 `operations` 卷，独立运行时应设置持久路径。记录只包含操作 ID、时间、固定错误码和计数。未完成记录表示没有保存完成回执，重试前应核对原进程及目标数据。

使用 `python -m sync_server operator-id --username <已固定账户名>` 获取账户的稳定 `user_id`，将获准账户 ID 填入部署配置 `SYNC_OPERATOR_USER_IDS`，多个 ID 用逗号分隔，再重启服务，即可管理服务和查看记录。账户改名后权限保持，设备撤销和会话过期仍会阻止访问。默认不开放管理权限。**服务管理**同时提供账户管理、分页只读恢复记录和依赖故障提示；CLI 可用 `operation-records --before <next_before>` 查看更早记录。恢复仍要求数据库和对象桶为空。

升级前结束或取消未完成上传，在新目录创建备份，执行 `verify-backup`，核对已完成回执。替换服务时保留原数据库、对象桶和运维记录。需要恢复时，在目标配置中选择独立的空数据库和空对象桶，执行 `restore` 后检查 `/ready`。接入两台隔离客户端，同步新文件并在第二台核对字节，再选择恢复后的实例投入使用。新备份包含账户策略与不可变管理回执；缺少这些增量表的旧备份仍可恢复。

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

## 存储与上传管理 API

通过 `GET /sync/v1/vaults/{vault_id}/usage` 读取存储账目。`logical_file_bytes` 按当前文件计数，对象分类对相同内容只计一次。`charged_bytes` 沿用原配额账目，`accounting_matches` 核对它与已登记对象的总量，不会默默改账。上传过期后不再预留配额，但暂存清理成功前仍会出现在列表中。

使用 `GET /sync/v1/vaults/{vault_id}/uploads?limit=30&before={cursor}` 查看本账户各设备的未完成上传，包括设备、确认偏移、声明大小、期限与撤销状态。知识库所有者可通过 `POST .../uploads/{upload_id}/cancel` 取消一个未完成上传；原传输接口继续核对原设备。取消不会删除已完成的对象，已经完成的上传会返回其完成结果。

取消请求中断后，先读取 `GET .../uploads/{upload_id}/result` 再决定是否重试。结果区分有效、已取消、已完成、已过期、已损坏、已回收、回收待核对和未知 ID，取消回执保持不可变。暂存删除失败会保留可重试记录。自动过期清理记录文件系统/元数据失败分类、耗时和累计计数；只有配置的 operator 账户可读取 `GET /sync/v1/admin/maintenance`。`GET .../retention` 提供本知识库的无限历史与手动回收规则。回执、摘要和回收表采用增量迁移，缺少它们的旧备份仍可恢复。

## 本地开发

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

## 安全与参与贡献

- 不得提交 `.env`、Token、密码、数据库、对象内容、备份或用户 Vault。
- 限制数据库和对象存储网络，启用监控，并及时撤销遗失设备。
- 路径规范化、操作 ID、基础修订、配额、内容哈希和账户边界均属于安全控制。
- 漏洞按照 [SECURITY.md](SECURITY.md) 私下报告。
- 贡献遵循[贡献指南](CONTRIBUTING.md)和[社区行为准则](CODE_OF_CONDUCT.md)。
- 使用仓库 Issue 表单和 PR 模板，并对基础设施与账户信息脱敏。

## 开源协议

本项目采用 [MIT License](LICENSE)，第三方组件继续适用各自的许可证与声明。
