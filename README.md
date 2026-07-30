# Telegram Customer Service Bot

这是一个基于 FastAPI、Telegram Webhook 和 SQLite 的双向留言 Bot。用户通过 Bot 留言，无需直接私聊个人账号；管理员会收到带历史记录的通知，并可接管会话持续回复。

当前版本不包含广告或 AI 内容审核。垃圾消息通过可选 Cloudflare Turnstile、发送频率限制和管理员手动黑名单处理，避免自动内容审核误伤正常用户。

## 主要功能

### 用户侧

- `/start` 留言入口
- 可选首次留言前 Cloudflare Turnstile 人机验证
- 支持文字、图片、文件、语音、视频等消息
- 消息送达确认
- 编辑消息会更新原历史记录，并通知管理员“用户修改了消息”
- 短时间刷屏会进入临时冷却，但不会自动加入黑名单
- 只处理 Telegram 私聊，不处理群聊或频道消息

### 管理员侧

- 新消息通知按“用户、本条内容、最近记录”分区，不重复展示当前消息
- `/start` 打开留言工作台，可用按钮切换待处理、超时、已处理和最近用户
- `/inbox` 显示尚未处理的新消息，`/pending` 显示超时待处理会话
- 队列和最近用户每页显示 10 条，内容与操作按钮使用相同编号
- 时间按 `DISPLAY_TIMEZONE` 转换后展示，数据库仍保存 UTC 时间
- 按钮颜色按用途区分：蓝色为主要操作、绿色为完成或恢复、红色为退出、取消或风险确认
- `/closed` 显示最近已处理会话，可通过按钮重新打开
- 持续回复模式，支持文字和媒体
- 多管理员接管与标记已处理
- 手动加入或解除黑名单，按钮操作需要二次确认
- `OWNER_IDS` 负责人专用群发与管理员审计权限
- 群发二次确认、后台发送、进度记录、失败用户重试和服务重启续发
- `/audit` 保存最近管理员接管、回复、关闭、拉黑和群发操作
- Telegram API 失败会写入 systemd 日志

普通用户的 Telegram 输入框菜单只显示 `/start`。每个 `ADMIN_IDS` 管理员会获得独立的完整管理菜单；运行 `scripts/manage_webhook.py` 时会自动同步，也可使用 `--commands-only` 单独更新菜单。

### 可靠性与安全

- 强制校验 `WEBHOOK_SECRET`
- Webhook 只接受 JSON，应用层限制为 1 MiB，Nginx 部署模板同步限制请求大小
- Turnstile Token 与 Telegram Mini App 身份均在服务端验证
- 所有管理命令和管理按钮均校验 `ADMIN_IDS`
- Webhook 更新使用 `processing/done/failed` 状态，失败后允许 Telegram 重试
- Telegram 触发 flood control 时按 `retry_after` 等待；群发失败用户可单独重试
- Telegram HTTP 连接复用
- 管理员通知自动控制在 Telegram 消息长度限制内
- SQLite WAL、自动迁移、在线备份和可选历史数据保留
- 支持按 GitHub Release 版本安装、更新和查看当前部署版本
- 更新失败时自动恢复代码、依赖、systemd 配置和 SQLite 数据库
- 数据库与备份目录在 Linux 上强制使用私有权限（文件 `600`、目录 `700`）
- `/healthz` 同时检查数据库和群发后台任务
- systemd 示例只监听 `127.0.0.1:9000`

## 技术要求

- Debian 或 Ubuntu VPS
- Python 3.10 或更高版本
- Nginx
- systemd
- 一个已经指向 VPS 的域名

部署脚本不假设 VPS 用户名。它按以下顺序确定运行 Bot 的非 root 用户：

1. 显式传入的 `APP_USER`
2. 已有 `tg-bot.service` 中的 `User`
3. 发起 `sudo` 的 `SUDO_USER`

如果直接登录 root 且尚未安装服务，必须显式指定，例如 `sudo APP_USER=ubuntu bash scripts/install.sh`。

## 安装

以运行 Bot 的普通 VPS 用户登录，只执行这一行：

```bash
curl -fsSL https://raw.githubusercontent.com/Shikinami05/TelegramCustomerServiceBot/main/scripts/bootstrap.sh | sudo bash
```

安装脚本支持：

- Debian 和 Ubuntu
- 自动识别运行 Bot 的普通 Linux 用户
- 全新安装及现有 `~/tg-bot` 安装迁移
- 自动选择最新稳定 GitHub Release
- 交互输入域名、证书邮箱、Bot Token 和管理员 ID
- 可选启用 Cloudflare Turnstile，并隐藏输入 Secret Key
- 自动创建 `.env`、随机 `WEBHOOK_SECRET` 和 Python 虚拟环境
- 自动配置 systemd、Nginx、HTTPS 证书、Webhook 和 Telegram 命令菜单
- HTTPS `443` 和 Telegram 支持的 `8443`
- 检测到 `443` 被 Xray 等程序占用时默认建议 `8443`
- 安装前测试及服务健康检查

真实 `.env` 和数据库只保存在 VPS，不会进入 GitHub。如果 `~/tg-bot` 已经是本项目，重复运行同一条安装命令会切换到公开 HTTPS 地址并安全更新；其他同名目录不会被覆盖。

## 管理脚本支持的命令

| 命令 | 功能和参数 |
| --- | --- |
| `sudo tg-bot update [latest\|v1.2.3]` | 更新到最新稳定版，或安装、回退到指定稳定版本；默认 `latest` |
| `sudo tg-bot backup [KEEP]` | 创建 SQLite 手动备份；默认保留最近 10 份 |
| `sudo tg-bot status` | 查看 systemd 服务状态和 `/healthz` |
| `sudo tg-bot restart` | 重启服务并等待健康检查通过 |
| `sudo tg-bot logs [LINES]` | 查看最近日志；默认显示 100 行 |
| `sudo tg-bot version` | 查看版本、Git 引用、提交和工作区状态 |
| `sudo tg-bot webhook` | 查看 Telegram Webhook 状态 |
| `sudo tg-bot turnstile status` | 查看 Turnstile 是否启用及配置完整性，不显示密钥 |
| `sudo tg-bot turnstile enable` | 交互式填写或保留 Turnstile 密钥并启用 |
| `sudo tg-bot turnstile disable` | 关闭 Turnstile，保留密钥便于以后重新启用 |
| `sudo tg-bot configure DOMAIN EMAIL [443\|8443]` | 配置 Nginx、HTTPS、Webhook 和命令菜单；默认端口 `443` |
| `sudo tg-bot help` | 显示脚本支持的全部命令 |

例如：

```bash
sudo tg-bot update
sudo tg-bot backup 20
sudo tg-bot logs 200
sudo tg-bot turnstile status
sudo tg-bot configure bot.example.com admin@example.com 8443
```

从 `v1.0.0` 或更早版本升级时，重新运行一次上面的安装命令；以后即可使用 `tg-bot` 管理脚本。

## 环境变量

复制参考文件时不要写入真实仓库：

```bash
cp .env.example .env
chmod 600 .env
```

必须配置：

```env
BOT_TOKEN=BotFather 提供的 Token
WEBHOOK_SECRET=随机长字符串
ADMIN_IDS=123456789,987654321
OWNER_IDS=123456789
WEBHOOK_URL=https://bot.example.com/tg/webhook
```

可选配置：

```env
DB_BACKUP_ENABLED=true
DB_BACKUP_INTERVAL_SECONDS=86400
DB_BACKUP_KEEP=14
DB_BACKUP_DIR=backups

USER_RATE_LIMIT_COUNT=8
USER_RATE_LIMIT_WINDOW_SECONDS=60
USER_RATE_LIMIT_COOLDOWN_SECONDS=300
MESSAGE_RETENTION_DAYS=180

BROADCAST_SEND_DELAY_SECONDS=0.05
BROADCAST_RATE_LIMIT_RETRIES=3
UPDATE_PROCESSING_TIMEOUT_SECONDS=300
PENDING_REMINDER_MINUTES=30
TELEGRAM_INLINE_RETRY_MAX_SECONDS=5

TURNSTILE_ENABLED=false
TURNSTILE_SITE_KEY=
TURNSTILE_SECRET_KEY=
TURNSTILE_VERIFY_URL=https://bot.example.com/verify
TURNSTILE_VERIFY_DAYS=30
TURNSTILE_INIT_DATA_MAX_AGE_SECONDS=600

DISPLAY_TIMEZONE=Asia/Hong_Kong
LOG_LEVEL=INFO
```

`MESSAGE_RETENTION_DAYS=0` 表示不自动清理历史消息。示例配置使用 `180` 天。

`OWNER_IDS` 可以省略；省略时所有 `ADMIN_IDS` 都视为负责人，以保持旧配置兼容。配置后，只有负责人能使用群发、群发失败重试和 `/audit`。`OWNER_IDS` 中的账号会自动获得管理员权限。

`PENDING_REMINDER_MINUTES` 控制 `/pending` 的超时阈值。普通消息只会在 `retry_after` 不超过 `TELEGRAM_INLINE_RETRY_MAX_SECONDS` 时短暂等待，避免 Webhook 长时间阻塞；后台群发不受这个短等待上限影响。

`DISPLAY_TIMEZONE` 使用 IANA 时区名称，只影响管理员界面的时间显示，不改变 SQLite 中的 UTC 时间。默认值为 `Asia/Hong_Kong`；例如可改为 `Asia/Shanghai` 或 `UTC`。无效名称会让服务在启动时直接报错，避免静默显示错误时间。

## Cloudflare Turnstile（可选）

安装时会询问：

```text
Enable Cloudflare Turnstile before users can leave messages? [y/N]
```

直接回车或选择 `n` 时功能保持关闭。选择 `y` 后，安装器会继续询问 Site Key，并隐藏输入 Secret Key。需要先在 Cloudflare Turnstile 创建 Managed Widget，把 Bot 域名加入允许列表。

启用后的流程：

1. 新用户发送 `/start` 或留言
2. Bot 只发送“完成人机验证”按钮，不保存本条内容，也不通知管理员
3. Telegram 内打开 `/verify` 页面并完成 Turnstile
4. 后端验证 Telegram `initData` 签名、时效、Turnstile Token、Action 和 Hostname
5. 验证状态写入 SQLite，默认有效 30 天
6. 验证通过后用户才能发送留言

管理员自动跳过验证。Turnstile Site Key 会出现在网页中，Secret Key 只保存在权限为 `600` 的 `.env`。`/verify/complete` 请求体限制为 16 KiB，验证页面禁止缓存并使用 CSP；Turnstile Token 必须经过 Cloudflare Siteverify 服务端确认。

Turnstile 不要求域名经过 Cloudflare 代理。如果域名启用了 Cloudflare Proxy、WAF 或 Under Attack Mode，必须确保 `/tg/webhook` 跳过所有交互式挑战，否则 Telegram 无法投递 Webhook。

现有安装启用时：

```bash
sudo tg-bot update
sudo tg-bot turnstile enable
```

脚本会隐藏 Secret Key 输入，并从现有 Webhook 地址生成验证地址。可随时查看状态或关闭：

```bash
sudo tg-bot turnstile status
sudo tg-bot turnstile disable
```

启用前脚本会确认 Nginx 已包含验证路由；如果提示路由不存在，先执行一次 `sudo tg-bot configure DOMAIN EMAIL [443|8443]`。配置修改后会重启服务并检查 `/healthz`，失败时自动恢复原 `.env`。关闭功能只修改开关，不会删除 Site Key 或 Secret Key。

## 管理员命令

```text
/myid
/inbox
/pending
/closed
/users
/reply 用户ID 内容
/send 用户ID 内容
/cancel
/takeover 用户ID
/close 用户ID
/blacklist 用户ID 可选原因
/unblacklist 用户ID
/blacklist_list
```

负责人额外拥有：

```text
/broadcast 内容
/broadcast_status
/broadcast_retry 任务ID
/audit
```

待处理流程：

1. 用户发送新消息后，会话进入 `/inbox` 并累加待处理数
2. 超过 `PENDING_REMINDER_MINUTES` 仍未处理时会出现在 `/pending`
3. 管理员成功回复后待处理数清零
4. 点击“标记已处理”或使用 `/close 用户ID` 后进入 `/closed`
5. 用户再次留言会自动重新打开，也可以由管理员手动重新打开

群发流程：

1. 管理员发送 `/broadcast 内容`
2. Bot 显示预计人数和确认按钮
3. 管理员确认后任务进入后台队列
4. Bot 完成后发送成功和失败数量
5. `/broadcast_status` 可查看最近进度
6. 失败用户可点击“重试失败用户”或使用 `/broadcast_retry 任务ID`

群发接收人会在确认时生成快照。黑名单用户不会进入快照；发送期间新加入黑名单的用户也不会收到群发。

## 数据库

数据库文件默认为：

```text
bot.db
```

主要数据表：

- `users`：用户资料和最近消息
- `admin_states`：管理员当前回复目标
- `message_logs`：用户和管理员回复历史
- `blacklists`：手动黑名单
- `conversations`：会话接管、待处理数和最后回复时间
- `admin_audit_logs`：管理员操作审计记录
- `pending_broadcasts`：群发任务状态和统计
- `broadcast_recipients`：群发接收人及发送结果
- `processed_updates`：Webhook 幂等和失败重试状态
- `user_rate_limits`：用户临时频率限制
- `user_verifications`：Turnstile 验证状态和有效期

旧数据库会在启动时自动增加新字段，不需要删除 `bot.db`。

## 备份

Bot 启动时会创建一次 SQLite 在线备份，之后按配置周期备份：

```text
backups/bot-YYYYMMDD-HHMMSS.db
```

默认保留最近 14 份。代码更新前，`scripts/update.sh` 也会先备份数据库。

需要立即创建一份手动备份时：

```bash
sudo tg-bot backup
```

手动备份保存在 `backups/manual/`，默认保留最近 10 份。可以用 `sudo tg-bot backup 20` 修改保留数量。

每次更新还会强制创建独立的回滚备份：

```text
backups/rollback/rollback-时间-提交.db
```

默认保留最近 5 份，可在执行更新时通过 `ROLLBACK_BACKUP_KEEP` 调整。

本机备份不能替代异地备份；重要数据应定期同步到另一台机器或对象存储。

## 更新

更新到最新稳定 Release：

```bash
sudo tg-bot update
```

安装或回退到指定 Release：

```bash
sudo tg-bot update v1.3.0
```

更新前脚本会检查 Git 工作区、获取目标提交并创建数据库回滚备份。随后更新依赖、运行测试、同步 systemd 服务并检查 `/healthz`。任一步失败都会自动停止服务并恢复：

- 更新前 Git 提交和分支状态
- 原版本 Python 依赖
- 原 systemd 服务文件
- 更新前 SQLite 数据库

自动回滚完成后还会再次检查健康状态。若回滚本身未能完整完成，脚本以状态码 `70` 退出并给出需要检查的 `journalctl` 命令。服务正常后，Telegram 菜单同步失败只会记录警告，不触发代码回滚。

## 版本

查看当前部署版本、Git 引用、提交和工作区状态：

```bash
sudo tg-bot version
```

正式 Tag 部署会显示例如 `v1.3.0`；开发分支会显示例如 `v1.3.0+提交号`。

## 发布 Release

`VERSION` 保存当前语义化版本。准备新版本时，先通过 PR 更新该文件并合并到 `main`，然后在干净且与 `origin/main` 一致的本地仓库运行：

```bash
bash scripts/create-release.sh v1.3.0
```

脚本会验证版本、运行测试、创建 annotated Tag 并推送。`.github/workflows/release.yml` 会再次验证 Tag 和测试结果，然后使用仓库内置 `GITHUB_TOKEN` 创建带自动发行说明的 GitHub Release。

## Webhook

重新配置 HTTPS、Webhook 和命令菜单：

```bash
sudo tg-bot configure bot.example.com admin@example.com 8443
```

查看当前状态：

```bash
sudo tg-bot webhook
```

允许的 Telegram 更新类型：

```json
["message", "edited_message", "callback_query"]
```

当使用 `8443` 时，Webhook URL 形如：

```text
https://bot.example.com:8443/tg/webhook
```

## 健康检查

```bash
sudo tg-bot status
```

正常返回：

```json
{"ok":true,"version":"1.3.0","db":"ok","broadcast_worker":"ok"}
```

Nginx 模板不会把 `/healthz` 暴露到公网。

Telegram API 错误日志只记录方法、HTTP 状态和错误描述，不记录包含 Bot Token 的请求 URL。

## 测试

```bash
PROJECT_DIR="$(sudo systemctl show tg-bot -p WorkingDirectory --value)"
cd "$PROJECT_DIR"
./venv/bin/python -m py_compile app.py scripts/manage_webhook.py scripts/manage_backup.py scripts/manage_turnstile.py
./venv/bin/python -m unittest discover -s tests -v
```

GitHub Actions 会在 Python 3.10 和 3.12 上自动执行这些检查，不需要生产环境密钥。

## GitHub 安全检查

以下内容绝不能提交：

- `.env` 或其他真实环境配置
- `bot.db`、`bot.db-wal`、`bot.db-shm`
- `backups/`
- `venv/` 或 `.venv/`
- 日志文件

当前 `.gitignore` 已覆盖这些文件。公开仓库前还应选择许可证，并把文档中的真实域名替换为示例域名。
