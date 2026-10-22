import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from tg_bot import database


BUILTIN_RULES_TEXT = (
    "<b>内置识别范围</b>\n"
    "博彩导流：哈希竞猜、链上赌场等主题，配合充值、返佣、进群等招揽内容。\n"
    "刷单与招募：日结刷单、跑分招募、成品号交易等话术，配合联系、领取等行动要求。\n\n"
    "<b>这些不会单独触发</b>\n"
    "哈希、USDT、兼职、链接等普通词语不会单独被当作广告；举报、求助和讨论优先保留。\n\n"
    "<b>示例话术</b>\n"
    "「幸运哈希开桌，首充有奖励，进群领取额度。」\n"
    "「刷单日结，联系领取任务。」\n\n"
    "上述文案与组合规则由本项目独立编写，没有导入第三方词库。"
)

GAMBLING = ("哈希竞猜", "哈希博彩", "哈希单双", "哈希大小", "幸运哈希", "区块链彩票", "时时彩", "哈希投注", "哈希下注", "链上赌场")
SOLICITATION = ("注册送", "充值送", "首充", "返佣", "代理招募", "下注", "投注", "包赔", "返水", "秒提", "进群", "联系客服", "入群领取", "充值奖励")
SCHEMES = ("刷单返佣", "跑分招募", "出售成品号", "免押金兼职", "日结刷单", "成品号批发", "刷单日结")
CONTACT = ("联系", "私聊", "加群", "进群", "招募", "领取")
DISCUSSION = ("有人发", "有人给", "收到广告", "收到的广告", "收到一条", "举报", "屏蔽", "拦截", "诈骗", "别信", "不要相信", "怎么防", "如何防", "是什么意思", "是什么", "这是广告", "骗局", "是真的吗")
LINKS = re.compile(r"(?<![\w@.-])(?:https?://[^\s<>\"'，。！？（）]+|(?:[\w-]+\.)+[\w-]{2,63}(?:/[^\s<>\"'，。！？（）]*)?)", re.IGNORECASE)


def normalize(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold()
                   if not char.isspace() and unicodedata.category(char) != "Cf")


def read_rules(db_path: Path) -> list[tuple[str, str]]:
    return [(row["kind"], row["value"]) for row in database.fetchall(
        db_path, "SELECT kind,value FROM moderation_rules ORDER BY kind,value")]


def format_rules(db_path: Path) -> str:
    return "\n".join(("词语：" if kind == "phrase" else "域名：") + value for kind, value in read_rules(db_path))


def replace_rules(db_path: Path, text: str) -> int:
    if len(text) > 3000:
        raise ValueError("规则总长度不能超过 3000 个字符")
    rules: set[tuple[str, str]] = set()
    if text.strip() != "清空":
        for line in text.splitlines():
            if not line.strip():
                continue
            kind, separator, value = line.strip().replace("：", ":", 1).partition(":")
            value = value.strip()
            if not separator or kind not in {"词语", "域名"} or not 2 <= len(value) <= 160:
                raise ValueError("每行使用「词语：广告短语」或「域名：example.com」，内容长度为 2 至 160 字符")
            if kind == "域名":
                try:
                    value = value.rstrip(".").encode("idna").decode("ascii").lower()
                except UnicodeError as exc:
                    raise ValueError("域名格式不正确") from exc
                labels = value.split(".")
                if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
                    raise ValueError("只填写域名，不包含协议、端口或路径")
                rules.add(("domain", value))
            else:
                if len(normalize(value)) < 2:
                    raise ValueError("广告短语至少包含两个可见字符")
                rules.add(("phrase", value))
    if len(rules) > 30:
        raise ValueError("最多保存 30 条自定义规则")
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM moderation_rules")
        conn.executemany("INSERT INTO moderation_rules(kind,value) VALUES(?,?)", sorted(rules))
        conn.commit()
    return len(rules)


def check(db_path: Path, text: str) -> str | None:
    normalized = normalize(text)
    rules = read_rules(db_path)
    for kind, value in rules:
        if kind == "domain":
            link_text = "".join(char for char in unicodedata.normalize("NFKC", text)
                                if unicodedata.category(char) != "Cf")
            for token in LINKS.findall(link_text):
                try:
                    token = token.rstrip(".,!?;:)]}")
                    host = urlsplit(token if "://" in token else "https://" + token).hostname or ""
                    host = host.rstrip(".").encode("idna").decode("ascii").lower()
                except (ValueError, UnicodeError):
                    continue
                if host == value or host.endswith("." + value):
                    return "命中自定义广告域名"
    # Reports and discussions go to AI; a keyword alone must not block ordinary chat.
    if any(word in normalized for word in DISCUSSION):
        return None
    if any(kind == "phrase" and normalize(value) in normalized for kind, value in rules):
        return "命中自定义广告短语"
    if any(topic in normalized and any(word in normalized.replace(topic, "") for word in SOLICITATION)
           for topic in GAMBLING):
        return "疑似博彩推广话术"
    if any(word in normalized for word in SCHEMES) and any(word in normalized for word in CONTACT):
        return "疑似返佣或招募推广"
    return None
