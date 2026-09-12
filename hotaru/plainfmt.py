from __future__ import annotations

from html.parser import HTMLParser

_KEEP = {"b", "strong", "i", "em", "u", "ins", "s", "del", "code", "pre", "spoiler", "tg-spoiler", "blockquote", "a", "br"}


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.lists: list[tuple[str, int]] = []
        self.skip = False

    def _emit(self, chunk: str) -> None:
        self.out.append(chunk)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        at = dict(attrs)
        if tag in _KEEP:
            self._emit(self.get_starttag_text() or f"<{tag}>")
        elif tag in ("p", "section", "details"):
            pass
        elif tag == "summary":
            self._emit("<b>")
            if self.out and self.out[-1] not in ("\n", "<br>") and any(self.out):
                self.out.insert(-1, "\n")
        elif tag in ("tg-emoji", "tg-time", "tg-button", "button", "tg-button-row"):
            if tag == "tg-emoji":
                self.skip = True
        elif tag in ("ul", "ol"):
            self.lists.append((tag, 0))
        elif tag == "li":
            self._emit("\n")
            if self.lists:
                kind, idx = self.lists.pop()
                idx += 1
                self.lists.append((kind, idx))
                self._emit(f"{idx}. " if kind == "ol" else "• ")
            else:
                self._emit("• ")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("<b>")
        elif tag in ("figcaption", "cite"):
            self._emit("<i>")
        elif tag == "aside":
            self._emit("<blockquote>")
        elif tag in ("img", "video"):
            src = at.get("src", "")
            self._emit(f'<a href="{src}">{src}</a>' if src else "")
        elif tag == "tg-map":
            self._emit("[map]")
        elif tag == "tg-math":
            self._emit("<code>")
        elif tag == "table":
            self._emit("<pre>")
        elif tag == "tr":
            self._emit("\n")
        elif tag in ("td", "th"):
            self._emit(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _KEEP:
            self._emit(f"</{tag}>")
        elif tag in ("p", "section", "details"):
            self._emit("<br>")
        elif tag == "summary":
            self._emit("</b><br>")
        elif tag in ("tg-emoji", "tg-time", "tg-button", "button", "tg-button-row"):
            if tag == "tg-emoji":
                self.skip = False
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._emit("<br>")
        elif tag == "li":
            pass
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("</b><br>")
        elif tag == "figcaption":
            self._emit("</i><br>")
        elif tag == "cite":
            self._emit("</i>")
        elif tag == "aside":
            self._emit("</blockquote>")
        elif tag == "tg-math":
            self._emit("</code>")
        elif tag == "table":
            self._emit("</pre>")
        elif tag in ("tg-collage", "tg-slideshow"):
            pass

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        self._emit(data)


def rich_to_plain(html: str) -> str:
    parser = _Converter()
    parser.feed(html)
    parser.close()
    text = "".join(parser.out)
    while text.endswith("<br>"):
        text = text[: -len("<br>")]
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines)
