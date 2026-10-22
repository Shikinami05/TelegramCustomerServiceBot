import html
from html.parser import HTMLParser


class PlainTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_plain_text(value: str) -> str:
    parser = PlainTextHTMLParser()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1] + "…"


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def escape_html_limited(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    escaped = html.escape(value)
    if utf16_length(escaped) <= limit:
        return escaped

    low = 0
    high = len(value)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = html.escape(value[:middle]) + "…"
        if utf16_length(candidate) <= limit:
            low = middle
        else:
            high = middle - 1
    return html.escape(value[:low]) + "…"
