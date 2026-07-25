# Telegram 双向留言 Bot

这是一个基于 FastAPI、Telegram Webhook 和 SQLite 的双向留言 Bot。用户通过 Bot 留言，无需直接私聊个人账号；管理员会收到带历史记录的通知，并可接管会话持续回复。

当前版本不包含广告或 AI 内容审核。垃圾消息通过发送频率限制和管理员手动黑名单处理，避免自动审核误伤正常用户。

## 主要功能

### 用户侧

- `/start` 留言入口
- 支持文字、图片、文件、语音、视频等消息
- 消息送达确认
- 编辑消息会更新原历史记录，并通知管理员“用户修改了消息”
- 短时间刷屏会进入临时冷却，但不会自动加入黑名单
- 只处理 Telegram 私聊，不处理群聊或频道消息

### 管理员侧

- 新消息通知按“用户、本条内容、最近记录”分区，不重复展示当前消息
- `/start` 打开留言工作台，可用按钮切换待处理、超时、已处理和最近用户
- `/inbox` 显示尚未处理的新消息，`/pending` 显示超时待处理会话
- 队列内容与操作按钮使用相同编号，便于在手机上快速对应
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
- 所有管理命令和管理按钮均校验 `ADMIN_IDS`
- Webhook 更新使用 `processing/done/failed` 状态，失败后允许 Telegram 重试
- Telegram 触发 flood control 时按 `retry_after` 等待；群发失败用户可单独重试
- Telegram HTTP 连接复用
- 管理员通知自动控制在 Telegram 消息长度限制内
- SQLite WAL、自动迁移、在线备份和可选历史数据保留
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

## 从 GitHub 安装

建议先使用私有仓库。VPS 使用只读 Deploy Key 拉取代码，`.env` 和数据库只保存在 VPS。

先以将要运行 Bot 的普通 VPS 用户登录。此时 `~` 就是这个用户真实的主目录，不需要写死 `/home/用户名`：

```bash
sudo apt-get update
sudo apt-get install -y git

mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/tg-bot-deploy -C tg-bot-deploy
cat ~/.ssh/tg-bot-deploy.pub
```

把公钥添加为 GitHub 仓库的只读 Deploy Key，再为这个专用密钥配置 SSH 别名：

```sshconfig
Host github-tg-bot
    HostName github.com
    User git
    IdentityFile ~/.ssh/tg-bot-deploy
    IdentitiesOnly yes
```

将上面的内容加入 `~/.ssh/config`，并设置权限：

```bash
chmod 600 ~/.ssh/config
ssh -T git@github-tg-bot
git clone git@github-tg-bot:OWNER/REPOSITORY.git ~/tg-bot
cd ~/tg-bot

sudo bash scripts/install.sh
sudo bash scripts/configure-nginx.sh bot.example.com admin@example.com
```

`install.sh` 会从 `SUDO_USER` 识别当前普通用户；安装完成后，其他脚本会优先读取 systemd 服务中的真实用户。若仓库需要安装给另一个用户，请明确运行 `sudo APP_USER=目标用户 bash scripts/install.sh`。

安装脚本会：

- 安装 Python 与虚拟环境组件
- 创建 `venv`
- 安装锁定版本的依赖
- 在首次安装时交互式创建 `.env`
- 自动生成随机 `WEBHOOK_SECRET`
- 将 `.env` 权限设置为 `600`
- 安装并启动 systemd 服务
- 运行语法检查和单元测试
- 检查 `/healthz`

`configure-nginx.sh` 会配置 Nginx、通过 Webroot 申请 HTTPS 证书、更新 `.env` 中的 `WEBHOOK_URL`，并设置 Telegram Webhook 与输入框命令菜单。默认使用 HTTPS `443`。

对于已经存在的证书，新版 Certbot 会通过 `reconfigure` 把后续续期验证改为 Webroot；旧版 Certbot 会保留 Nginx 插件作为续期兼容兜底，不会强制重复签发证书。

如果 `443` 已被 Xray REALITY 等服务占用，使用 Telegram 支持的 `8443`：

```bash
sudo bash scripts/configure-nginx.sh bot.example.com admin@example.com 8443
```

脚本会在修改 Nginx 前检查目标端口。发现端口由非 Nginx 程序占用时会停止，不会覆盖现有代理服务。使用 `8443` 时还需要在 UFW 和 VPS 服务商防火墙中允许 TCP `8443`。

脚本在 GitHub 上没有可执行权限时，可始终使用 `bash scripts/install.sh` 运行。也可以在 VPS 执行：

```bash
chmod +x scripts/*.sh
```

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
LOG_LEVEL=INFO
```

`MESSAGE_RETENTION_DAYS=0` 表示不自动清理历史消息。示例配置使用 `180` 天。

`OWNER_IDS` 可以省略；省略时所有 `ADMIN_IDS` 都视为负责人，以保持旧配置兼容。配置后，只有负责人能使用群发、群发失败重试和 `/audit`。`OWNER_IDS` 中的账号会自动获得管理员权限。

`PENDING_REMINDER_MINUTES` 控制 `/pending` 的超时阈值。普通消息只会在 `retry_after` 不超过 `TELEGRAM_INLINE_RETRY_MAX_SECONDS` 时短暂等待，避免 Webhook 长时间阻塞；后台群发不受这个短等待上限影响。

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

旧数据库会在启动时自动增加新字段，不需要删除 `bot.db`。

## 备份

Bot 启动时会创建一次 SQLite 在线备份，之后按配置周期备份：

```text
backups/bot-YYYYMMDD-HHMMSS.db
```

默认保留最近 14 份。代码更新前，`scripts/update.sh` 也会先备份数据库。

本机备份不能替代异地备份；重要数据应定期同步到另一台机器或对象存储。

## 更新

GitHub 仓库没有本地改动时：

```bash
PROJECT_DIR="$(sudo systemctl show tg-bot -p WorkingDirectory --value)"
cd "$PROJECT_DIR"
sudo bash scripts/update.sh
```

更新脚本使用 `git pull --ff-only`，遇到本地代码修改会停止，不会强制覆盖。它会同步仓库中的 systemd 服务模板并执行 `daemon-reload`，让后续安全加固真正应用到现有 VPS。服务通过健康检查后会自动同步普通用户和管理员的 Telegram 输入框命令菜单；菜单同步失败只记录警告，不影响已经成功的代码更新。

## Webhook

设置或更新：

```bash
APP_USER="$(sudo systemctl show tg-bot -p User --value)"
PROJECT_DIR="$(sudo systemctl show tg-bot -p WorkingDirectory --value)"
sudo runuser -u "$APP_USER" -- "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/scripts/manage_webhook.py"
```

查看当前状态：

```bash
sudo runuser -u "$APP_USER" -- "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/scripts/manage_webhook.py" --info
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
curl --fail http://127.0.0.1:9000/healthz
```

正常返回：

```json
{"ok":true,"db":"ok","broadcast_worker":"ok"}
```

Nginx 模板不会把 `/healthz` 暴露到公网。

Telegram API 错误日志只记录方法、HTTP 状态和错误描述，不记录包含 Bot Token 的请求 URL。

## 测试

```bash
PROJECT_DIR="$(sudo systemctl show tg-bot -p WorkingDirectory --value)"
cd "$PROJECT_DIR"
./venv/bin/python -m py_compile app.py scripts/manage_webhook.py
./venv/bin/python -m unittest discover -s tests -v
```

GitHub Actions 会在 Python 3.10 和 3.12 上自动执行这些检查，不需要生产环境密钥。

## 运维命令

```bash
sudo systemctl status tg-bot --no-pager
sudo systemctl restart tg-bot
journalctl -u tg-bot -n 100 --no-pager
journalctl -u tg-bot -f
sudo nginx -t
sudo systemctl reload nginx
```

## GitHub 安全检查

以下内容绝不能提交：

- `.env` 或其他真实环境配置
- `bot.db`、`bot.db-wal`、`bot.db-shm`
- `backups/`
- `venv/` 或 `.venv/`
- 日志文件

当前 `.gitignore` 已覆盖这些文件。公开仓库前还应选择许可证，并把文档中的真实域名替换为示例域名。
