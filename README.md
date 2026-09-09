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
2. 执行 `docker compose up -d`。一次性 `initialize` 服务等待依赖后幂等创建 schema 与 `opennexus` Bucket；重复运行只检查并补齐缺失资源，不覆盖已有行或对象。长期运行的 `sync` 服务继续使用只限该 Bucket 的同步账号，不使用 MinIO root 身份。
3. 执行 `docker compose run --rm sync /service/.venv/bin/python -m sync_server create-user`，密码交互输入，不放命令参数。
4. 使用 Caddy 示例配置 TLS。8080 仅绑定本机，不直接公开明文 HTTP。
5. 检查 `/health`、`/ready` 及经过授权的上传/读取；`/ready` 探测数据库 schema、staging 读写和对象存储测试前缀。

`initialize` 命令已通过真实 PostgreSQL/MinIO 的空实例与重复运行验证，并由 Compose 的一次性服务调用。MinIO 同步账号仍须由管理员创建并限制到 `opennexus` Bucket，`.env` 中的 root 与同步凭据必须不同。

2026-09-08 已在独立 Docker 项目完成真实 PostgreSQL/MinIO 双 worker 测试部署，修正基础镜像中的 `sync` 系统用户名冲突。测试专用 HTTP 地址、故障检查、完整验收缺口与运维入口见[验收报告](../docs/development/OpenNexus验收报告-2026-09-08.md)。S-07 已在原生 PostgreSQL 17.11/MinIO 实例完成 1 GiB/10,000 文件的删除源实例与空实例恢复；当前机器没有 Docker CLI，因此修改后的 Compose 编排仍需在发布环境复演，生产 TLS 也仍是独立发布门。

## 备份与空实例恢复

`backup` 在 PostgreSQL 的同一只读 repeatable-read 事务中导出 schema v1 的全部表和对象清单，再流式下载清单中的不可变对象并核对长度/SHA-256。存在未完成上传或缺失历史对象时失败；目标目录必须不存在。备份目录含认证哈希和会话哈希，必须使用受限 ACL、加密磁盘及异机副本保护。

```powershell
python -m sync_server backup --directory D:/OpenNexus-backups/2026-09-09 --io-workers 8
```

`restore` 默认拒绝超过 24 小时的备份，只允许空 PostgreSQL 数据库和空/不存在 Bucket。命令先校验完整备份，再上传并回读核对全部对象，最后在单个 PostgreSQL 事务中创建 schema、导入并复核对象目录；数据库不会引用只恢复一部分的对象。恢复演练应使用新实例，成功后再启动服务并检查 `/ready`。

```powershell
python -m sync_server restore --directory D:/OpenNexus-backups/2026-09-09 --io-workers 8
```

生产定时任务至少每日生成一次新目录并检查命令退出码、`created_utc`、对象数与总字节；保留策略和异机复制由部署维护者配置。升级前执行新备份并完成抽样恢复。Schema v1 拒绝未知数据库版本，不自动降级；当前历史永久保留，容量管理不能手动删除被历史引用的对象。


## 四并发大附件传输探针

仅对可保留测试数据的隔离实例运行。准备两个已有测试账号的受限权限 JSON 文件，内容为两个含 username/password 字段的对象数组；不要将该文件提交到仓库。工具会创建四个测试 Vault，保留数据供进一步核对，不修改已有 Vault。

```powershell
uv run python tools/upload_benchmark.py --url http://localhost:8080 --allow-test-http --credentials C:/private/sync-test-accounts.json --output ../.build/upload-report.json
```

默认两账号各两个 Vault，以共同启动屏障进行四个 100 MiB 上传，逐块确认 offset，重复 complete 和 revision 检查持久回执，再流式下载核对大小/SHA256并检查跨账号拒绝。无自动重试掩盖失败，报告不含凭据、令牌或响应正文。HTTP 必须显式指定测试选项；HTTPS 使用默认验证且不跟随重定向。单次运行上限 30 分钟。

报告 result 表示本次传输检查结果，acceptance 始终 NOT_ASSESSED：工具尚未接入服务进程/容器 RSS 采集器，也未执行完整 S-09 的 30 分钟提交负载、10000 笔记和规定网络条件。真实 TCP 回环回归见 tests/test_upload_benchmark.py，其中 SQLite/DiskObjects 与客户端位于同一 Python 进程，不能作为生产性能指标。
