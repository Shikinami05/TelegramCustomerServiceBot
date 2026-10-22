# Telegram Customer Service Bot

这是一个用于个人聊天的 Telegram 双向中转 Bot。别人向 Bot 发消息，你直接 Reply 或选择联系人持续回复，无需公开个人私聊入口。

默认启用本地广告规则，可选用 DeepSeek 或硅基流动辅助判断。疑似广告静默进入垃圾箱，正常聊天通过持久化消息映射转发。

## 主要功能

- 首页只有「聊天、垃圾箱、设置」。
- 聊天分「未读 / 全部」，支持分页、原生 Reply 和持续回复；退出回复按钮为红色。
- 打开联系人成功后标记所显示批次为已读；查看当前联系人详情不会退出持续回复。返回首页、聊天列表、设置或切换其他联系人时，会明确结束或切换回复目标。
- 已读与回复分开记录，回复期间收到的新消息不会被一起清零。多个管理员的回复目标独立保存，未读状态共享。
- 聊天列表支持「全部已读」，覆盖所有页；操作后到达的新消息仍保留未读。
- 会话「更多」中可删除会话，二次确认后从所有管理员的列表移除并退出对应的持续回复。只整理列表，不清除历史、原生 Reply 映射或 Telegram 消息，不等于拉黑；对方再次留言后重新出现。确认期间有新留言时不会删除。
- 用户详情保留数字 ID、用户名和聊天记录，日常通知以昵称和消息为主。
- 保留 `/myid`，欢迎页保留「如何留言 / 支持格式」。
- 支持文字、图片、视频、文件、音频、语音、动画、贴纸、位置和联系人；保留 caption。
- 回复成功优先使用消息 reaction；不可用时回退为简短提示，发送失败时明确说明。
- 保留拉黑、解除拉黑和设置中的拉黑列表。
- 移除群发、接管、工单关闭、重开、超时队列及审计页面。旧按钮和旧指令不能继续执行这些操作。
- 本地规则支持博彩推广等组合话术，以及自定义广告短语、域名。规则命中后暂存，不自动永久封禁。
- AI 审核按「正常、疑似广告、待确认」处理；网络或 API 故障单独标注，不当作广告。
- 广告不会逐条通知管理员，也不向用户解释审核结果、服务商或模型。检测故障只在状态变化且节流允许时提醒。
- 欢迎页是统一留言聊天入口；只处理私聊，不处理群聊或频道消息。

### 可靠性与安全

- 强制验证 Webhook secret，管理操作校验 `ADMIN_IDS`，AI 配置仅允许 `OWNER_IDS`。
- Webhook 只接受 JSON，限制请求大小；systemd 只监听 `127.0.0.1:9000`。
- 消息映射、回复对象、入站去重和投递任务保存在 SQLite，重启后仍可回复之前的消息。
- 投递结果不确定时保留状态，不盲目自动重放；支持告警后的人工重试。
- SQLite WAL、自动迁移、在线备份、历史保留和私有文件权限。
- 支持按 Release 安装、升级前备份、升级失败回滚。
- `/healthz` 检查数据库、管理员消息投递和审核后台任务。
- 日志不记录 API 密钥或完整私人聊天内容。

## 技术要求

- Debian 或 Ubuntu VPS
- Python 3.10 或更高版本
- Nginx
- systemd
- 一个已经指向 VPS 的域名

## 项目结构

项目保留 `uvicorn app:app` 入口，同时把可独立测试的基础能力放入 `tg_bot/`：

```text
app.py                 FastAPI 生命周期、Webhook、消息与回调编排
tg_bot/config.py       环境变量解析、类型转换和启动校验
tg_bot/database.py     SQLite 连接、Schema、迁移和通用查询
tg_bot/telegram.py     Telegram Bot API HTTP 传输和错误模型
tg_bot/keyboards.py    Inline Keyboard、分页、按钮颜色和回调数据
tg_bot/models.py       共享结果类型
tg_bot/text.py         HTML 与 Telegram 文本长度处理
tg_bot/repositories/   Update、已读、映射、投递和广告规则的 SQLite 事务
tg_bot/ai_providers.py 固定服务商端点、模型预设和兼容参数
tg_bot/services/       投递后台任务、本地规则、AI 审核和垃圾箱交互
scripts/               安装、更新、备份、Webhook 和运维命令
tests/                 业务、模块、安全边界和部署脚本回归测试
```

当前仍使用 FastAPI、HTTPX 和 SQLite，不依赖 Aiogram、Redis、PostgreSQL 或 ORM。项目继续保留原有部署入口，并通过启动迁移兼容旧数据库。

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
- 可选启用 AI 防广告，选择服务商，隐藏输入 API Key，设置模型、每日调用上限和无文字媒体策略
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
| `sudo tg-bot moderation status` | 查看 AI 审核开关、模型及每日调用上限，不显示密钥 |
| `sudo tg-bot moderation enable` | 选择 AI 服务商，隐藏输入密钥，设置模型、限额和无文字媒体策略 |
| `sudo tg-bot moderation disable` | 关闭新消息 AI 审核，保留密钥和待人工处理的历史消息 |
| `sudo tg-bot configure DOMAIN EMAIL [443\|8443]` | 配置 Nginx、HTTPS、Webhook 和命令菜单；默认端口 `443` |
| `sudo tg-bot help` | 显示脚本支持的全部命令 |

例如：

```bash
sudo tg-bot update
sudo tg-bot backup 20
sudo tg-bot logs 200
sudo tg-bot moderation status
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

UPDATE_PROCESSING_TIMEOUT_SECONDS=300
TELEGRAM_INLINE_RETRY_MAX_SECONDS=5
ADMIN_REPLY_STATE_TTL_SECONDS=1800

SPAM_RULES_ENABLED=true
AI_MODERATION_ENABLED=false
AI_PROVIDER=deepseek
AI_API_KEY=
AI_MODEL=deepseek-flash
MODERATION_MEDIA_POLICY=hold
MODERATION_TIMEOUT_SECONDS=15
MODERATION_DAILY_LIMIT=500

DISPLAY_TIMEZONE=Asia/Hong_Kong
LOG_LEVEL=INFO
```

`MESSAGE_RETENTION_DAYS=0` 表示不自动清理历史消息。示例配置使用 `180` 天。

`OWNER_IDS` 可以省略；省略时所有 `ADMIN_IDS` 都视为负责人，以保持旧配置兼容。配置后，只有负责人能配置服务商、密钥、模型及广告规则。`OWNER_IDS` 中的账号会自动获得管理员权限。

普通消息只会在 `retry_after` 不超过 `TELEGRAM_INLINE_RETRY_MAX_SECONDS` 时短暂等待，避免 Webhook 长时间阻塞。

`DISPLAY_TIMEZONE` 使用 IANA 时区名称，只影响管理员界面的时间显示，不改变 SQLite 中的 UTC 时间。默认值为 `Asia/Hong_Kong`；例如可改为 `Asia/Shanghai` 或 `UTC`。无效名称会让服务在启动时直接报错，避免静默显示错误时间。

## AI 防广告（可选）

负责人在 Telegram 发送 `/ai`，或从「设置 → 防广告」进入。普通管理员只能处理暂存消息，不能查看或修改密钥。

首页只选一种模式：

| 模式 | 新留言如何处理 |
| --- | --- |
| 仅规则 | 本地判断明显广告，不调用 AI，无需密钥 |
| 规则 + AI | 先运行本地规则，再由已配置的 AI 判断其余文字 |
| 暂停防广告 | 直接接收新留言；历史垃圾箱不会自动清空或放行 |

第一次使用 AI：点击「配置 AI」→ 阅读发送数据与密钥风险说明 →「输入密钥并启用」。密钥与所选模型验证成功后即启用「规则 + AI」，无需再找开关或重启。默认使用 DeepSeek；需要换服务商、模型、每日限额或无文字媒体策略时再进入「更多设置」。已有密钥时选择「规则 + AI」会先确认，再验证并启用。

模型按钮提供常用预设，也可输入完整模型 ID；硅基流动支持如 `Qwen/Qwen3-32B` 这样的带斜杠 ID。中国站和国际站使用各自的端点和密钥，不能混用。

密钥输入需先确认风险。提示有效期 3 分钟，回复提示或直接发送均可，`/cancel` 取消。Bot 先删除输入再验证密钥；删除或验证失败不更新配置。切换服务商会暂停 AI、清空旧密钥并使旧输入失效，本地规则仍按当前状态生效，防止把一个平台的密钥提交给另一个平台。「更多设置 → API 密钥」只替换密钥，不改变当前模式；快捷配置入口明确确认后才自动启用。

密钥不会写入消息历史、操作日志或转发给留言用户。输入元数据会保存在数据库以防重启后误路由，密钥本身只保存在 `.env`。配置提示的过期回复和疑似密钥的管理员消息也会被拦截，避免进入持续回复流程。

「连接检查」验证鉴权及所选模型是否在服务商列表中；「测试检测」经确认后发送内置测试文本，验证实际调用与 JSON 解析，计入每日调用限额。两者都不保证广告识别准确率。Telegram Bot 私聊不是端到端加密通道，删除输入不能保证清除通知副本；更稳妥的方式是在 VPS 隐藏输入密钥：

```bash
sudo tg-bot moderation enable
```

默认 DeepSeek 模型为 `deepseek-flash`，硅基流动默认 `Qwen/Qwen3-32B`。模型以服务商当前可用列表为准，不承诺永久可用。默认每日最多调用 500 次，以 UTC 日期结算；限制的是请求数，不是固定金额。每次输入最多 8000 字符、输出最多 128 tokens。仅使用预置官方 HTTPS 端点，不跟随重定向，不允许模型调用工具。参数依照 [DeepSeek 官方文档](https://api-docs.deepseek.com/api/create-chat-completion/) 和 [SiliconFlow 官方文档](https://docs.siliconflow.com/en/api-reference/chat-completions/chat-completions) 按服务商分别发送。

处理流程：

1. 黑名单和发送频率限制优先执行，管理员回复不会送去 AI 审核。
2. 用户消息先持久化，再由后台检测；Webhook 不等待 AI 返回。用户只收到普通留言回执。
3. 先运行本地规则，未命中的内容在开启 AI 时再交给模型。正常内容进入聊天，疑似广告进入垃圾箱；不确定内容和故障在「待确认」中显示。
4. 从首页打开垃圾箱，进入单条详情后可放行、忽略、重新检测或预览。忽略只结束该条处理，不封禁用户；封禁必须二次确认。操作后保留分类和页码。详情记录检测时的服务商与请求模型，不用当前设置冒充历史来源。
5. 疑似广告不主动通知；检测故障状态变化时每 10 分钟最多提醒一次，不逐条推送广告正文。
6. 编辑消息重新审核；转发使用审核过的文字、caption 和文件 ID 快照，避免之后的修改绕过审核。旧版未发出的任务会取消。
7. 重启保留审核记录，审核中断的消息等待人工确认。暂停 AI 后，本地规则仍按独立开关生效，未命中规则的新消息直接接收；历史暂存不会自动放行。

本地规则默认开启，无需 API Key。在「设置 → 防广告 → 广告规则」中编辑自定义规则，每行填写 `词语：广告短语` 或 `域名：example.com`；发送「清空」删除自定义规则。最多 30 条，总长度不超过 3000 字符。域名按主机名和子域名匹配，词语按归一化后的短语匹配，不接受正则表达式。内置规则组合判断博彩、返佣和招募话术，单独出现「哈希」「USDT」「合作」不会直接拦截；举报和讨论类内容继续交给 AI 判断（AI 暂停时直接接收）。规则和 AI 都可能误判，暂存内容可以手动放行。

### 原创规则说明

内置规则、页面说明和下列示例均由本项目独立编写，没有复制第三方词库、广告样本或大型正则，也不会在更新时下载外部规则覆盖你的自定义内容。

- 博彩导流：博彩主题与充值、返佣、进群等招揽内容同时出现。例如「幸运哈希开桌，首充有奖励，进群领取额度。」会暂存。
- 刷单与招募：具体推广话术与联系、领取等行动要求组合。例如「刷单日结，联系领取任务。」会暂存。
- 正常聊天：单独提到哈希、USDT、兼职或分享一个链接，不会因此被内置规则屏蔽；明确举报和讨论广告的留言优先保留。
- 自定义补充：只有你明确添加的广告短语或域名才会加入自定义规则，示例域名不会自动生效。自定义规则在「仅规则」和「规则 + AI」模式生效。

可参考 [tg-spam 的规则与人工纠错设计](https://github.com/umputun/tg-spam)，以及 [Tg-Ad-RegEx 对中文 Telegram 广告类型的整理](https://github.com/AzurLab/Tg-Ad-RegEx)。后者更新较早，群管理规则也比个人聊天激进；这里仅提供设计参考，不照搬其屏蔽范围或实现。这不是 Telegram 官方词库，也不能保证拦住所有广告。

规则开关为 `SPAM_RULES_ENABLED`，与 `AI_MODERATION_ENABLED` 独立。通过 Telegram 修改规则立即生效。规则本身存入 SQLite，会随数据库备份；不需要把规则文件上传到外部服务。

当前只检测文字、caption 和隐藏链接，不下载链接、不上传媒体，不做图片 OCR 或语音识别。开启 AI 时无文字媒体默认暂存；只有本地规则且 AI 暂停时直接接收；可在「防广告 → 更多设置 → 无文字媒体」中改为直接接收，但纯图片广告也可能通过。带正常 caption 的媒体仍可能含未识别广告，AI 也可能误判。待处理队列最多 2000 条，满载时明确提示用户稍后重发。

启用后，留言中的文字及链接会发送给所选服务商，可能含用户自行写入的个人信息；不额外提交 Telegram ID、用户名或完整历史。普通欢迎语及回执不显示审核或模型信息，运营者应自行提供适当的隐私说明并确认第三方处理符合使用场景。密钥只保存在权限 `600` 的 `.env`，不要提交 GitHub。

```bash
sudo tg-bot moderation status
sudo tg-bot moderation disable
```

管理脚本修改配置后重启并检查健康状态，失败时恢复旧配置。暂停拦截保留当前密钥，切换服务商清空密钥。旧 `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` 继续兼容；如同时配置新旧变量，`AI_API_KEY` / `AI_MODEL` 优先。

交互设计参考 [aiogram-dialog](https://github.com/Tishka17/aiogram_dialog) 的页面与状态分离、[grammY menu](https://grammy.dev/plugins/menu.html) 的原消息更新与返回路径，以及 [tg-spam](https://github.com/umputun/tg-spam) 的静默拦截与人工纠错思路。没有引入这些框架或照搬其实现，原有 FastAPI、SQLite 与持久化消息映射保留。

CF 人机验证功能已移除，旧 `TURNSTILE_*` 环境变量不再读取；配置 AI 审核时会清除这些旧变量。旧数据库验证表保留但不再使用，避免升级时破坏历史数据。旧 Nginx 验证路径即使保留也只会返回 404，无需为了启用审核重新申请证书或修改端口。

## 管理员命令

Telegram 输入框菜单保留：

```text
/start       打开聊天首页
/cancel      退出持续回复或取消输入
/myid        查看自己的 Telegram ID
/settings    打开设置
```

普通用户菜单只有 `/start`。更新时会同步各管理员的命令菜单；`/ai`、`/inbox`、`/users`、`/moderation` 保留为旧入口兼容，但不再挤占主菜单。

日常聊天：

1. 用户发送消息，规则或 AI 检查后，正常内容进入聊天。
2. 直接 Reply 用户通知或媒体，即可回复对应联系人。
3. 从「聊天」打开联系人，或点击「持续回复」，之后发送的文字和媒体会发给该联系人；页面明确显示当前对象。
4. 点击「退出回复」或使用 `/cancel` 结束持续回复。闲置超过 `ADMIN_REPLY_STATE_TTL_SECONDS` 自动失效。
5. 已读只推进到已显示或明确回复的消息批次，后来收到的消息仍是未读。
6. 「更多」提供拉黑操作；「用户详情」保留 ID 和最近记录。

管理员通知在数据库队列中投递。网络结果不确定时，负责人会收到人工重试入口，重试前应核对是否已经送达。

旧群发记录保留在原数据库中，所有未完成群发在升级时停用，服务不会恢复发送。

## 数据库

数据库文件默认为：

```text
bot.db
```

主要数据表：

- `users`：用户资料和最近消息
- `admin_states`：管理员当前回复目标
- `message_logs`：用户和管理员回复历史
- `message_links`：用户消息与每个管理员聊天中的通知、媒体和回复消息映射
- `inbound_events`：用户入站 Update 的幂等事件记录
- `admin_deliveries`：发往管理员的通知和媒体持久化投递队列
- `admin_reply_deliveries`：管理员发往用户的回复投递台账和未知状态
- `blacklists`：手动黑名单
- `conversations`：聊天、已读序号、未读数和最后回复时间
- `admin_audit_logs`：管理员操作审计记录
- `processed_updates`：Webhook 幂等和失败重试状态
- `user_rate_limits`：用户临时频率限制
- `moderation_jobs`：审核消息快照、状态和人工决定
- `moderation_usage`：按 UTC 日期记录的 API 请求数
- `moderation_notices`：检测故障提醒节流
- `moderation_rules`：自定义广告短语与域名
- `admin_config_inputs`：负责人配置提示、有效期和使用状态，不保存密钥或输入内容

旧数据库会在启动时自动增加新字段，不需要删除 `bot.db`。
超过 `MESSAGE_RETENTION_DAYS` 的消息历史、消息映射和已结束投递台账会在维护任务中清理；待发送、发送中及仍有关联的入站事件不会被删除。

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
sudo tg-bot update v1.6.1
```

更新前脚本会检查 Git 工作区、获取目标提交并创建数据库回滚备份。随后更新依赖、运行测试、同步 systemd 服务并检查 `/healthz`。任一步失败都会自动停止服务并恢复：

- 更新前 Git 提交和分支状态
- 原版本 Python 依赖
- 原 systemd 服务文件
- 更新前 SQLite 数据库

自动回滚完成后还会再次检查健康状态。若回滚本身未能完整完成，脚本以状态码 `70` 退出并给出需要检查的 `journalctl` 命令。服务正常后，Telegram 菜单同步失败只会记录警告，不触发代码回滚。

从 `v1.6.1` 或更早版本升级到 AI 审核版，请直接更新到 `v1.7.2` 或更新的版本。`v1.7.0` 和 `v1.7.1` 删除了旧更新器仍会检查的 CF 脚本，可能在升级时失败并自动回滚。`v1.7.2` 保留一个仅提示功能已移除的兼容文件，不恢复 CF 验证。

更新成功会自动重启 Bot。若 `/ai` 仍提示未知指令，先运行 `sudo tg-bot version` 查看磁盘版本，再运行 `curl -fsS http://127.0.0.1:9000/healthz` 查看运行版本。若运行版本落后，执行 `sudo tg-bot restart`；若两者都还是旧版，需重新完成更新。升级后发送 `/start` 获取新首页。`/ai` 能打开但命令菜单未更新时，以运行 Bot 的普通用户在项目目录执行 `./venv/bin/python scripts/manage_webhook.py --commands-only`。

## 版本

查看当前部署版本、Git 引用、提交和工作区状态：

```bash
sudo tg-bot version
```

正式 Tag 部署会显示例如 `v1.6.1`；开发分支会显示例如 `v1.6.1+提交号`。

## 发布 Release

`VERSION` 保存当前语义化版本。准备新版本时，先通过 PR 更新该文件并合并到 `main`，然后在干净且与 `origin/main` 一致的本地仓库运行：

```bash
bash scripts/create-release.sh v1.6.1
```

脚本会验证版本、运行测试、创建 annotated Tag 并推送。`VERSION` 合并到 `main` 后，如果对应 Tag 尚不存在，`.github/workflows/release.yml` 也会自动创建 annotated Tag；随后再次验证 Tag 和测试结果，并使用仓库内置 `GITHUB_TOKEN` 创建带自动发行说明的 GitHub Release。

本机无法连接 GitHub 时，也可以在仓库的 `Actions` 页面选择 `Release`，点击 `Run workflow` 并输入与 `VERSION` 一致的版本号。工作流只从 `main` 创建经过校验的 annotated Tag，再执行同一套测试和 Release 发布流程。

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
{"ok":true,"version":"当前版本","db":"ok","moderation_worker":"ok","admin_delivery_worker":"ok"}
```

Nginx 模板不会把 `/healthz` 暴露到公网。

Telegram API 错误日志只记录方法、HTTP 状态和错误描述，不记录包含 Bot Token 的请求 URL。

## 测试

```bash
PROJECT_DIR="$(sudo systemctl show tg-bot -p WorkingDirectory --value)"
cd "$PROJECT_DIR"
./venv/bin/python -m compileall -q tg_bot
./venv/bin/python -m py_compile app.py scripts/manage_webhook.py scripts/manage_backup.py scripts/manage_moderation.py
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
