"""Extract visible HTML content without executing scripts or loading assets."""

from html.parser import HTMLParser
import re


class _Reader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag in {"p", "div", "section", "article", "li", "tr", "br", "pre"}:
            self.parts.append("\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag in {"p", "div", "section", "article", "li", "tr", "pre"} or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def visible_text(html):
    reader = _Reader()
    reader.feed(html)
    return "\n".join(line for raw in "".join(reader.parts).splitlines()
                     if (line := re.sub(r"[\t \u00a0]+", " ", raw).strip()))
