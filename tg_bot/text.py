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


def escape_html_limited(value: str, limit: int) -> str:
    escaped = html.escape(value)
    if len(escaped) <= limit:
        return escaped

    low = 0
    high = len(value)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = html.escape(value[:middle]) + "…"
        if len(candidate) <= limit:
            low = middle
        else:
            high = middle - 1
    return html.escape(value[:low]) + "…"
