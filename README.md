# Telegram Customer Service Bot

這是一個用於個人聊天的 Telegram 雙向中轉 Bot。別人向 Bot 留言，你透過原生 Reply 或明確開啟持續回覆繼續聊天，不必公開個人私聊入口。

預設啟用本地廣告規則，可選用 DeepSeek 或矽基流動進行語意審核。疑似廣告安靜暫存，正常聊天透過持久化訊息映射轉送。

## 主要功能

- Bot 介面統一繁體中文，包括歡迎頁、按鈕、指令選單、通知和錯誤提示。聯絡人的訊息、暱稱、既有歷史及自訂規則保留原文，不做簡繁轉換。
- 首頁固定顯示「未讀留言、所有會話、全部標為已讀、設定、重新整理」，分開統計未讀會話與未讀訊息；沒有未讀時也保留已讀按鈕。
- 列表頂部同樣提供一鍵全部已讀及重新整理，支援分頁。批次已讀涵蓋所有頁，不只目前頁面；操作後到達的新留言仍保留未讀。
- 查看會話不會自動進入持續回覆。顯示成功後只標記本次看到的留言為已讀；明確點選「持續回覆」才會轉送之後輸入的訊息，紅色「退出回覆」結束該模式。
- 查看目前聯絡人或重新整理會保留已開啟的持續回覆；切換其他聯絡人、返回首頁或列表、進入設定及更多操作時會先退出舊回覆對象。舊退出按鈕不會取消另一個人的會話。
- 一鍵已讀與刪除會話分開：已讀只清除未讀計數，保留聊天記錄。這是 Bot 內部的閱讀狀態，不是代替你操作 Telegram 客戶端的未讀角標。
- 查看、持續回覆、退出及更多操作保留來源列表和頁碼。標記舊留言已讀後會刷新目前頁面，但不順便讀掉新留言；原始轉發通知只更新按鈕，不覆寫正文或破壞 Reply 映射。
- 會話「更多操作」可刪除會話，二次確認後從所有管理員的聊天列表移除，並退出對應的持續回覆。保留現有歷史、原生 Reply 映射和 Telegram 訊息；不等於封鎖。對方再次留言後恢復會話；確認期間有新留言時不會刪除。
- 負責人可從「所有會話」底部刪除所有會話；確認範圍涵蓋所有頁，有效期 3 分鐘，重啟後仍有效且只能使用一次。確認期間新增或有新留言的會話保留，歷史、原生 Reply 映射、封鎖與暫存留言均不刪除。
- 使用者詳情保留數字 ID、使用者名稱、最近留言、回覆狀態及聊天記錄，日常通知以暱稱和內容為主。
- 保留 `/myid`，歡迎頁保留「如何留言 / 支援格式」。支援文字、圖片、影片、檔案、音訊、語音、動畫、貼圖、位置和聯絡人，保留 caption。
- 多個管理員的持續回覆對象獨立保存，未讀狀態共用。回覆成功優先使用 reaction，無法使用時回退為簡短提示；傳送失敗有明確說明。
- 設定保留封鎖名單、暫存留言與既有防廣告入口，不在聊天首頁堆放設定。網路或 API 故障與廣告分開，普通使用者不會收到判定、服務商或模型資訊。
- 只處理私聊，不處理群組或頻道。群發、接管、工單關閉、重開、逾時佇列及審計頁面已移除，舊按鈕與指令也不能執行。

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
tg_bot/ui.py           繁體中文文案、首頁、閱讀概況和會話版面
tg_bot/models.py       共享结果类型
tg_bot/text.py         HTML 与 Telegram 文本长度处理
tg_bot/repositories/   Update、已读、映射、投递和广告规则的 SQLite 事务
tg_bot/ai_providers.py 固定服务商端点、模型预设和兼容参数
tg_bot/services/       投递后台任务、本地规则、AI 审核和垃圾箱交互
tg_bot/services/moderation_prompt.py  原創審核提示詞、範例與嚴格回傳格式
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

## AI 防廣告（可選）

負責人從「設定 → 防廣告」或 `/ai` 進入。主頁只顯示當前需要的動作：未設定密鑰時「設定密鑰並啟用」、已設定但關閉時「啟用 AI 語意審核」、啟用後「暫停 AI 審核」。服務商、模型與密鑰集中在「連線設定」；限額、規則及無文字媒體放在「更多設定」。普通管理員只能處理暫存留言，不能管理密鑰。

| 狀態 | 新留言如何處理 |
| --- | --- |
| 僅本地規則 | 本地判斷，無需密鑰，不呼叫 AI |
| AI 語意審核 | 啟用中的自訂封鎖規則直接暫存；其餘文字由 AI 判斷，內建規則只提供線索 |
| 暫停所有審核 | 直接接收新留言，既有暫存不會自動放行或清除 |

「暫停 AI」不會順便關閉本地規則；完整暫停或切回「僅本地規則」可在更多設定選擇。`SPAM_RULES_ENABLED` 與 `AI_MODERATION_ENABLED` 是獨立開關；本地規則關閉時，自訂規則也不生效。

### 設定與檢查

首次設定會先說明第三方處理與密鑰風險，驗證密鑰和模型成功後立即啟用，無需重啟。密鑰輸入有效期 3 分鐘，可用 `/cancel` 取消；刪除輸入或驗證失敗均不更新設定。切換服務商會暫停 AI、清空舊密鑰並使舊輸入失效，避免跨平台洩漏。單獨更新密鑰不改變模式。

支援 DeepSeek、矽基流動中國站與國際站；模型可選預設或輸入完整 ID，例如 `Qwen/Qwen3-32B`。不同站點的密鑰不能混用，模型以服務商目前可用列表為準。

- **檢查連線**：驗證鑑權與模型列表，不代表模型能正確分類廣告。
- **合成樣例測試**：經確認後傳送兩條原創合成文字（一般聊天、明確博彩招攬），顯示各自是否符合預期。占用 2 次每日額度，可能產生費用；不使用私人留言，也不代表整體準確率。
- **判定標準**：直接查看審核範圍、規則優先序、資料範圍與提示詞版本，不必閱讀原始碼。

密鑰只保存在權限 `600` 的 `.env`，不進入聊天歷史、操作日誌或使用者通知。Telegram Bot 私聊不是端對端加密，刪除輸入無法保證清除通知副本；較穩妥的方式是在 VPS 隱藏輸入：

```bash
sudo tg-bot moderation enable
```

### 判定流程與邊界

1. 黑名單與頻率限制優先；管理員回覆不送審。使用者留言先持久化，再由背景工作處理，Webhook 不等待 AI，使用者只收到普通留言回執。
2. 啟用本地規則時，自訂封鎖短語或域名命中後直接暫存，不交給 AI 推翻。AI 開啟時，內建組合規則僅作提示，由模型複核整體意圖；僅本地規則模式則直接依規則處理。
3. 原創提示詞區分主動招攬與聊天、合理接洽、分享及舉報。單獨出現哈希、USDT、兼職、連結或 @帳號不等於廣告；沒有連結也可能是招攬。普通問候不因缺少歷史而進入待確認。
4. 模型只能輸出 `verdict` 和 `reason_code`。程式驗證固定代碼及其對應關係，拒絕多餘欄位、重複鍵或任意理由；原因文案由程式生成。模型不能執行封鎖或其他操作，也不輸出沒有校準的信心百分比。提示詞不能保證完全抵抗繞過或誤判。
5. 暫存區分為「疑似廣告、待確認、檢測故障」。逾時、額度不足、限流及格式錯誤不當作廣告；不會因此自動封鎖。詳情記錄當時的服務商、模型、提示詞版本與原因，舊記錄不假裝使用新版本。
6. 單條留言可放行、忽略、預覽或重新檢測；封鎖必須另行確認。放行不是自動訓練，也不把該使用者之後的留言全部免審。疑似廣告不主動推送；故障狀態改變時最多每 10 分鐘提醒一次。
7. 編輯後重新審核，轉送使用已審核文字、caption 和檔案 ID 快照。重啟保留記錄，中斷的檢測等待人工確認；切換模式不會自動放行既有暫存。

提示詞與範例集中在 `tg_bot/services/moderation_prompt.py`，由本專案獨立編寫。放行或忽略記錄不會直接拼入 system 提示詞，留言內的角色設定、「忽略規則」等內容只作待審資料。

### 自訂規則與資料範圍

「更多設定 → 自訂封鎖規則」每行填寫 `詞語：完整短語` 或 `域名：example.com`；傳送「清空」移除自訂規則。最多 30 條、總長度 3000 字元，詞語按歸一化短語匹配，域名包含子域名，不支援正則。自訂規則存入 SQLite 並隨資料庫備份；內建規則與範例沒有複製第三方詞庫，更新也不會下載外部規則覆蓋你的設定。

AI 僅收到這則留言的文字、caption 與隱藏連結目標，不額外提交 ID、暱稱或歷史。不開啟網址、不下載或上傳媒體，也沒有 OCR 或語音識別。留言本身仍可能包含個資，營運者應提供適當的隱私說明。無文字媒體在 AI 模式預設暫存，可在更多設定改為直接接收；純圖片及帶普通 caption 的廣告仍可能漏檢。僅本地規則模式直接接收無文字媒體。

預設每日最多 500 次，以 UTC 日期計算；限制的是請求數，不是固定金額。每次輸入最多 8000 字元、輸出最多 192 tokens；待處理佇列最多 2000 條。只使用預置 HTTPS 端點、不跟隨重導向、不提供工具。API 參數按服務商分別處理，參見 [DeepSeek 官方文件](https://api-docs.deepseek.com/api/create-chat-completion/) 與 [SiliconFlow 官方文件](https://docs.siliconflow.com/en/api-reference/chat-completions/chat-completions)。

```bash
sudo tg-bot moderation status
sudo tg-bot moderation disable
```

管理腳本修改後重啟並檢查健康，失敗恢復舊設定；Telegram 介面修改立即生效。舊 `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` 保持相容，新 `AI_API_KEY` / `AI_MODEL` 優先。

設計參考 [tg-spam](https://github.com/umputun/tg-spam) 的規則、模型與人工糾錯分工，以及 [aiogram-dialog](https://github.com/Tishka17/aiogram_dialog) 的頁面與狀態分離；沒有引入其框架或照搬其提示詞、詞庫與處置方式。保留 FastAPI、SQLite 及持久化訊息映射。

CF 驗證已移除，舊 `TURNSTILE_*` 不再讀取；舊資料表保留但不使用，升級不需重新申請證書或修改端口。

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
3. 從「未讀留言」或「所有會話」查看聯絡人，只會閱讀記錄，不會啟用轉送。需要連續聊天時明確點選「持續回覆」，頁面顯示目前對象後再傳送。
4. 點選紅色「退出回覆」或使用 `/cancel` 結束持續回覆；閒置超過 `ADMIN_REPLY_STATE_TTL_SECONDS` 自動失效。
5. 「全部標為已讀」在首頁及列表頂部，清除所有會話的未讀計數（含封鎖前留下的未讀狀態）但不刪除歷史或解除封鎖；之後到達的新留言仍是未讀。單條已讀只處理按鈕對應的訊息批次。
6. 「更多操作」提供刪除會話與封鎖，兩者分開確認；「使用者詳情」保留 ID 和最近記錄。負責人可在「所有會話」底部點選「刪除所有會話」，確認後批次整理列表，不刪除歷史；確認期間的新留言保留。這與一鍵已讀是不同操作。

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
- `conversation_cleanups`：批次刪除確認的負責人、會話序號快照、有效期與使用狀態，不保存訊息正文
- `admin_audit_logs`：管理员操作审计记录
- `processed_updates`：Webhook 幂等和失败重试状态
- `user_rate_limits`：用户临时频率限制
- `moderation_jobs`：審核訊息快照、狀態、人工決定，以及當時的服務商、模型、提示詞版本與固定原因代碼
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
