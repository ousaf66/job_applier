"""
Email rendering shared by the online (Vercel) version.

These functions are copied verbatim from send_applications.py — the same placeholder
rules, the same HTML + plain-text email, the same row checks — so an email sent from
Vercel is identical to one sent from the Mac. The only change: placeholder_values() and
its callers take an optional `now`, because a serverless function runs in UTC and the
{{date}} in a letter should be the sender's own date. test_parity.py proves they match.
"""

import html
import re
from datetime import datetime
from html.parser import HTMLParser


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


PLACEHOLDERS = ["company", "role", "contact_name", "greeting_name", "location", "source",
                "job_url", "notes", "sender_name", "sender_email", "phone", "linkedin",
                "github", "portfolio", "date"]


def placeholder_values(row, cfg, now=None):
    """What each {{field}} becomes for this row. Falls back sensibly on blanks."""
    contact = (row.get("contact_name") or "").strip()
    today = now or datetime.now()
    return {
        "company": (row.get("company") or "").strip() or "your company",
        "role": (row.get("role") or "the role").strip(),
        "contact_name": contact,
        "greeting_name": contact if contact else "Hiring Team",
        "location": (row.get("location") or "").strip(),
        "source": (row.get("source") or "").strip(),
        "job_url": (row.get("job_url") or "").strip(),
        "notes": (row.get("notes") or "").strip(),
        "sender_name": cfg.get("sender_name", ""),
        "sender_email": cfg.get("sender_email", ""),
        "phone": cfg.get("phone", ""),
        "linkedin": cfg.get("linkedin", ""),
        "github": cfg.get("github", ""),
        "portfolio": cfg.get("portfolio", ""),
        "date": f"{today.day} {today:%B %Y}",
    }


def fill(text, values, escape=False):
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", html.escape(val) if escape else val)
    return text


def render_subject(tpl, row, cfg, now=None):
    subject = fill(tpl["subject"], placeholder_values(row, cfg, now))
    return " ".join(subject.split())


def render_body(tpl, row, cfg, now=None):
    """The HTML body for this row: the greeting line, then the template body."""
    values = placeholder_values(row, cfg, now)
    out = ""
    greeting = tpl.get("greeting", "").strip()
    if greeting:
        out += "<p>%s %s,</p>\n" % (html.escape(greeting), html.escape(values["greeting_name"]))
    out += fill(tpl.get("html", ""), values, escape=True)
    return tidy(out)


def tidy(out):
    """A line whose only content was an empty placeholder (no portfolio, say)
    is dropped rather than left as a gap."""
    out = re.sub(r"(?:<br\s*/?>\s*)+(?=</(?:p|div|h\d|li)>)", "", out)
    out = re.sub(r"(?<=<p>)\s*(?:<br\s*/?>\s*)+", "", out)
    out = re.sub(r"(<br\s*/?>\s*){2,}", "<br>", out)
    out = re.sub(r"<p>\s*</p>\s*", "", out)
    return out


def template_problem(*texts):
    """Placeholders that survived rendering — misspelt, or split up by formatting."""
    joined = "\n".join(texts)
    leftover = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", joined))
    unknown = sorted(leftover - set(PLACEHOLDERS))
    if unknown:
        return "Template has unknown placeholders: " + ", ".join(unknown)
    if leftover or "{{" in joined or "}}" in joined:
        return ("Template has a broken placeholder — retype it so the {{ }} and the name "
                "inside have the same formatting.")
    return None


EMAIL_STYLE = "font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#222222"


def email_html(body):
    return ('<!doctype html>\n<html><head><meta charset="utf-8"></head>'
            '<body style="margin:0;padding:0"><div style="%s">\n%s\n</div></body></html>\n'
            % (EMAIL_STYLE, body))


class _TextOnly(HTMLParser):
    """HTML body -> the plain-text copy sent alongside it."""
    BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "table", "tr"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.lists, self.links, self.skip = [], [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self.out.append("\n\n----------\n\n")
        elif tag == "li":
            kind = self.lists[-1] if self.lists else ["ul", 0]
            kind[1] += 1
            mark = "%d. " % kind[1] if kind[0] == "ol" else "- "
            self.out.append("\n" + "   " * max(0, len(self.lists) - 1) + mark)
        elif tag == "a":
            self.links.append((dict(attrs).get("href") or "", len(self.out)))
        elif tag in self.BLOCKS:
            self.out.append("\n\n")
        if tag in ("ul", "ol"):
            self.lists.append([tag, 0])

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.links:
            href, start = self.links.pop()
            text = "".join(self.out[start:])
            bare = re.sub(r"^(https?://|mailto:)", "", href).rstrip("/")
            if href and bare and bare not in text:
                self.out.append(" (%s)" % href)
        elif tag in self.BLOCKS:
            self.out.append("\n\n")
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data.replace("\xa0", " ")))

    def text(self):
        lines = [ln.strip() for ln in "".join(self.out).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def html_to_text(body):
    parser = _TextOnly()
    parser.feed(body)
    parser.close()
    return parser.text()


def render_cover(cover, row, cfg, now=None):
    return tidy(fill(cover.get("html", ""), placeholder_values(row, cfg, now), escape=True))


def cover_filename(cfg):
    name = re.sub(r"[^A-Za-z0-9]+", "_", cfg.get("sender_name") or "").strip("_")
    return (name + "_" if name else "") + "Cover_Letter.pdf"


def check(row, seen):
    """Return a reason string if this row must be skipped, else None."""
    status = (row.get("status") or "").strip().lower()
    if status == "sent":
        return "already marked sent"
    if status in {"skip", "hold", "no"}:
        return "on hold"
    email = (row.get("email") or "").strip()
    if not email:
        return "no email address"
    if not EMAIL_RE.match(email):
        return f"malformed email ({email})"
    if (email.lower(), (row.get("company") or "").lower().strip()) in seen:
        return "already in sent_log.csv"
    return None


def label(row):
    """How a row is named in output: its company, or its address when there is none."""
    return (row.get("company") or "").strip() or (row.get("email") or "").strip() or "?"
