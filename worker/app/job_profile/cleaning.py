"""Conservative document cleanup; evidence spans address this normalized text."""
import hashlib
import html
import re
from html.parser import HTMLParser


def description_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DocumentParser(HTMLParser):
    BLOCKS = {"p", "div", "section", "article", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
    SKIP = {"script", "style", "nav", "footer", "form", "button", "noscript", "svg"}
    VOID = {"br", "hr", "img", "input", "meta", "link", "wbr", "source", "area", "embed"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []

    @property
    def suppressed(self):
        return any(blocked for _, blocked in self.stack)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        noise = " ".join(str(attributes.get(k) or "") for k in ("class", "id", "role"))
        blocked = (self.suppressed or tag in self.SKIP or "hidden" in attributes
                   or attributes.get("aria-hidden") == "true"
                   or bool(re.search(r"\b(?:cookie-banner|cookie-consent|navigation|navbar|session-token)\b", noise, re.I)))
        if tag not in self.VOID:
            self.stack.append((tag, blocked))
        if blocked:
            return
        if tag in self.BLOCKS or tag in {"br", "hr"}:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag == "td":
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        was_suppressed = self.suppressed
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if not was_suppressed and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def clean_description(value: str | None) -> str:
    if not value:
        return ""
    # Decode nested Greenhouse encodings, without unbounded recursive expansion.
    text = str(value)
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    parser = DocumentParser()
    parser.feed(text)
    parser.close()
    lines = []
    for line in "".join(parser.parts).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[^\S\n]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def description_quality(text: str, source_name: str) -> tuple[str, str]:
    if not text:
        return "empty", "no description text"
    if re.search(r"(?:read more|show more|zobrazit vice|zobrazit více|\.\.\.|…)\s*$", text, re.I):
        return "snippet", "truncated ending or read-more marker"
    if source_name == "jooble_direct":
        return "snippet", "Jooble snippet; completeness unverified"
    if re.fullmatch(r"(?:apply now|job not found|this job is no longer available|sign in|register)[.!\s]*", text, re.I):
        return "template", "application/withdrawal shell"
    if len(text) < 500 or len(text.split()) < 60:
        return "short", "short description; completeness unverified"
    return "complete", "substantial description without obvious truncation"
