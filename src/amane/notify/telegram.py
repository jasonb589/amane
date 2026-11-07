"""Telegram HTML 的长度限制与标签闭合; 长度按 UTF-16 单位保守计算."""

from __future__ import annotations

import html
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True)
class _Token:
    text: str
    tag: str = ""
    closing: bool = False


class _HTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[_Token] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "br":
            self.handle_data("\n")
        elif tag in {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "pre", "tg-spoiler"}:
            self.tokens.append(_Token(f"<{tag}>", tag))
        elif tag == "a" and values.get("href"):
            self.tokens.append(_Token(f'<a href="{html.escape(values["href"] or "", quote=True)}">', tag))
        elif tag == "code":
            language = values.get("class") or ""
            attr = f' class="{html.escape(language, quote=True)}"' if language.startswith("language-") else ""
            self.tokens.append(_Token(f"<code{attr}>", tag))
        elif tag == "blockquote":
            self.tokens.append(_Token("<blockquote expandable>" if "expandable" in values else "<blockquote>", tag))
        elif tag == "span" and values.get("class") == "tg-spoiler":
            self.tokens.append(_Token('<span class="tg-spoiler">', tag))
        elif tag == "tg-emoji" and (values.get("emoji-id") or "").isdigit():
            self.tokens.append(_Token(f'<tg-emoji emoji-id="{values["emoji-id"]}">', tag))

    def handle_endtag(self, tag: str) -> None:
        self.tokens.append(_Token(f"</{tag}>", tag, closing=True))

    def handle_data(self, data: str) -> None:
        self.tokens.append(_Token(data))


def text_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def clip_text(text: str, limit: int) -> str:
    if text_length(text) <= limit:
        return text
    return text.encode("utf-16-le")[: max(0, limit - 1) * 2].decode("utf-16-le", errors="ignore").rstrip() + "…"


def clip_html(text: str, limit: int) -> str:
    parser = _HTML()
    parser.feed(text)
    parser.close()
    truncated = sum(text_length(token.text) for token in parser.tokens if not token.tag) > limit
    remaining = max(0, limit - int(truncated))
    parts: list[str] = []
    stack: list[str] = []
    for token in parser.tokens:
        if token.tag:
            if token.closing:
                if token.tag in stack:
                    while stack:
                        tag = stack.pop()
                        parts.append(f"</{tag}>")
                        if tag == token.tag:
                            break
            elif remaining:
                parts.append(token.text)
                stack.append(token.tag)
            continue
        size = text_length(token.text)
        if size > remaining:
            part = token.text.encode("utf-16-le")[: remaining * 2].decode("utf-16-le", errors="ignore")
            parts.append(html.escape(part, quote=False))
            break
        parts.append(html.escape(token.text, quote=False))
        remaining -= size
    if truncated:
        parts.append("…")
    parts.extend(f"</{tag}>" for tag in reversed(stack))
    return "".join(parts)


def plain_html(text: str) -> str:
    parser = _HTML()
    parser.feed(text)
    parser.close()
    return "".join(token.text for token in parser.tokens if not token.tag)
