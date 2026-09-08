# OpenNexus Sync 服务原型

协议及限制见 [Sync v1](../docs/contracts/Sync-v1契约.md)。服务独立于 AI Core，生产入口仅支持 PostgreSQL 和 S3。当前尚未达到 M3 运维退出条件。

## 隔离测试

```powershell
cd 'server sync'
uv sync --frozen
uv run pytest
```

测试自动创建临时 SQLite、对象和暂存目录，不读用户 Vault。

## 自托管准备

1. 将 `.env.example` 复制为 `.env`，生成独立数据库、MinIO 管理和同步访问凭据。数据库 URL 使用 `postgresql+psycopg://…`，其中密码须 URL 编码。
2. 启动 `docker compose up -d database objects`。由管理员在 MinIO 建立 `notesagent` 私有 Bucket，并创建仅能访问该 Bucket 的同步账号；服务不使用 MinIO root 身份。
3. 执行 `docker compose run --rm sync /service/.venv/bin/python -m sync_server create-user`，密码交互输入，不放命令参数。
4. 启动 `docker compose up -d sync`；使用 Caddy 示例配置 TLS。8080 仅绑定本机，不直接公开明文 HTTP。
5. 检查 `/health`、`/ready` 及经过授权的上传/读取；`/ready` 探测数据库 schema、staging 读写和对象存储测试前缀。

Dockerfile/Compose 是待实测部署配置，不能视为已验证安装程序。未提供自动 MinIO 初始化和备份恢复命令，发布前必须完成。

2026-09-08 已在独立 Docker 项目完成真实 PostgreSQL/MinIO 双 worker 测试部署，修正基础镜像中的 `sync` 系统用户名冲突。测试专用 HTTP 地址、故障检查、完整验收缺口与运维入口见[验收报告](../docs/development/OpenNexus验收报告-2026-09-08.md)。仓库通用 Compose 的生产 TLS 与备份恢复仍未通过发布验收。

升级前同时备份 PostgreSQL 和 Bucket，停止提交以取得一致切点。Schema v1 拒绝未知数据库版本，不自动降级。当前历史永久保留，容量管理不能手动删除被历史引用的对象。


## 四并发大附件传输探针

仅对可保留测试数据的隔离实例运行。准备两个已有测试账号的受限权限 JSON 文件，内容为两个含 username/password 字段的对象数组；不要将该文件提交到仓库。工具会创建四个测试 Vault，保留数据供进一步核对，不修改已有 Vault。

```powershell
uv run python tools/upload_benchmark.py --url http://localhost:8080 --allow-test-http --credentials C:/private/sync-test-accounts.json --output ../.build/upload-report.json
```

默认两账号各两个 Vault，以共同启动屏障进行四个 100 MiB 上传，逐块确认 offset，重复 complete 和 revision 检查持久回执，再流式下载核对大小/SHA256并检查跨账号拒绝。无自动重试掩盖失败，报告不含凭据、令牌或响应正文。HTTP 必须显式指定测试选项；HTTPS 使用默认验证且不跟随重定向。单次运行上限 30 分钟。

报告 result 表示本次传输检查结果，acceptance 始终 NOT_ASSESSED：工具尚未接入服务进程/容器 RSS 采集器，也未执行完整 S-09 的 30 分钟提交负载、10000 笔记和规定网络条件。真实 TCP 回环回归见 tests/test_upload_benchmark.py，其中 SQLite/DiskObjects 与客户端位于同一 Python 进程，不能作为生产性能指标。
