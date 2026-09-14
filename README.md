# OpenNexus Server Sync

当前预发布版本为 **0.3.1-alpha.2**。协议及限制见 [Sync v1](../docs/contracts/Sync-v1契约.md)。服务独立于 AI Core，生产入口仅支持 PostgreSQL 和 S3 兼容对象存储。独立发布包包含服务源码、锁文件、Vue 3 + TypeScript 管理控制台静态文件、Dockerfile 与 Compose 模板，不包含任何 Vault、账户数据库、对象存储数据或部署密钥。

服务根路径 `/` 与 `/console/` 提供同源的 Vue 3 + TypeScript Sync Console，可查看服务健康与依赖就绪状态，并使用普通 Sync 账户管理自己的 Vault 和设备。页面只调用公开的 Sync v1 API；密码在请求发出前从输入框清除，访问和刷新令牌只保留在页面内存，刷新或关闭页面即丢弃。控制台源码位于 `console/`，生产静态文件由 Docker 多阶段构建生成。

```powershell
cd console
pnpm install --frozen-lockfile
pnpm build
```

## 隔离测试

```powershell
cd 'server sync'
uv sync --frozen
uv run pytest
```

测试自动创建临时 SQLite、对象和暂存目录，不读用户 Vault。

## 自托管准备

从发布页下载 `OpenNexus-Server-Sync-0.3.1-alpha.2.zip` 并核对 `SHA256.json` 后，将压缩包解压到独立目录。升级现有实例时先备份数据库、对象存储和 `.env`，再使用新版镜像替换 Sync 服务；不要用发行包覆盖持久化卷。

1. 将 `.env.example` 复制为 `.env`，生成独立数据库、MinIO 管理和同步访问凭据。数据库 URL 使用 `postgresql+psycopg://…`，其中密码须 URL 编码。
2. 执行 `docker compose up -d`。一次性 `initialize` 服务等待依赖后幂等创建 schema 与 `opennexus` Bucket；重复运行只检查并补齐缺失资源，不覆盖已有行或对象。长期运行的 `sync` 服务继续使用只限该 Bucket 的同步账号，不使用 MinIO root 身份。
3. 全新数据库会生成账户 `admin` 和本次启动专用的随机密码。使用 `docker compose logs sync` 查找 `SYNC_BOOTSTRAP_CREDENTIALS`；随机密码不会写入镜像、环境变量或数据库明文。只要账户尚未固定，服务每次重启都会更换该密码并撤销旧会话。
4. 使用随机密码首次登录控制台后，必须立即修改账户名和密码。保存成功后凭据写入数据库，此后服务重启不再更换。已有正式账户的升级实例不会额外创建默认账户。仍可使用 `create-user` 运维命令增加独立账户，密码通过终端交互输入。
5. 使用 Caddy 示例配置 TLS。默认通过 `SYNC_BIND_ADDRESS=127.0.0.1` 与
   `SYNC_PORT=8080` 只监听本机。仅限已授权的隔离测试阶段将监听地址改为
   `0.0.0.0` 并直接开放测试端口；该模式不作为生产发布配置。
6. 检查 `/health`、`/ready` 及经过授权的上传/读取；`/ready` 探测数据库 schema、staging 读写和对象存储测试前缀。

如需让保留旧数据的升级实例执行一次首次设置，可运行下列命令。它只新增临时管理员，不删除旧账户、Vault 或对象；命令输出的随机密码在固定前也会随服务重启而失效。

```powershell
docker compose run --rm sync /service/.venv/bin/python -m sync_server bootstrap-user
```

`initialize` 命令已通过真实 PostgreSQL/MinIO 的空实例与重复运行验证，并由 Compose 的一次性服务调用。MinIO 同步账号仍须由管理员创建并限制到 `opennexus` Bucket，`.env` 中的 root 与同步凭据必须不同。

0.3.1-alpha.2 已使用真实 PostgreSQL/MinIO 双 worker 环境验证初始化、重复启动、固定凭据、健康检查和已有数据升级。测试专用 HTTP 地址、故障检查、完整验收记录与运维入口见[验收报告](../docs/development/OpenNexus验收报告-2026-09-08.md)。S-07 已在原生 PostgreSQL 17.11/MinIO 实例完成 1 GiB/10,000 文件的删除源实例与空实例恢复。测试阶段可以直接开放 HTTP 端口；生产上线仍需配置 TLS、访问控制、监控与异机备份。

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
