#!/usr/bin/env python3
"""
Cover letter HTML -> PDF. Standard library only.

Understands the slice of HTML the Email / Cover editor produces: paragraphs, line
breaks, headings, bold, italic, underline, text colour, links, and bullet or
numbered lists. Everything is set in Helvetica on A4. Helvetica is built into every
PDF reader, so no font is embedded and a one-page letter comes out at a few KB.

    pdf = cover_pdf.render("<p>Dear Hiring Team,</p>...", title="Cover letter")
"""

import re
import zlib
from html.parser import HTMLParser

PAGE_W, PAGE_H = 595.28, 841.89             # A4, in points
MARGIN = 64
BODY_SIZE = 11
LEADING = 1.42                              # line height as a multiple of font size
PARA_GAP = 7                                # extra space after a paragraph
SIZES = {"h1": 20, "h2": 17, "h3": 13}
LIST_INDENT = 18
INK = (0.133, 0.133, 0.133)                 # #222, same as the emails
LINK_RGB = (0.102, 0.373, 0.839)            # #1a5fd6, same as the editor

# Advance widths in 1/1000 em for WinAnsi codes 32-126, from Adobe's Helvetica AFMs.
# The oblique faces share the upright widths.
REGULAR = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]
BOLD = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
]
# The cp1252 punctuation people actually type into a letter: (regular, bold).
EXTRA = {0x80: (556, 556), 0x85: (1000, 1000), 0x91: (222, 278), 0x92: (222, 278),
         0x93: (333, 500), 0x94: (333, 500), 0x95: (350, 350), 0x96: (556, 556),
         0x97: (1000, 1000), 0xA0: (278, 278), 0xA9: (737, 737), 0xAE: (737, 737),
         0xB7: (278, 278)}

BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre"}
BREAK = object()                            # a <br>
SPACE = object()                            # a gap between words
URLISH = re.compile(r"^(?:https?://\S+|(?:www\.)?[\w-]+(?:\.[\w-]+)*\."
                    r"(?:com|io|ai|dev|org|net|me|co|app|pk)(?:/\S*)?"
                    r"|[^@\s]+@[\w-]+(?:\.[\w-]+)+)$", re.I)


def encode(text):
    return text.replace(" ", " ").encode("cp1252", errors="replace")


def width(data, bold, size):
    """Width in points of cp1252 bytes."""
    table = BOLD if bold else REGULAR
    total = 0
    for b in data:
        if 32 <= b <= 126:
            total += table[b - 32]
        else:
            total += EXTRA.get(b, (556, 611))[1 if bold else 0]
    return total * size / 1000.0


def parse_color(value):
    value = (value or "").strip().lower()
    m = re.match(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", value)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    m = re.match(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", value)
    if m:
        return tuple(min(255, int(g)) / 255.0 for g in m.groups())
    return None


def full_url(href):
    href = (href or "").strip()
    if not href or re.match(r"^(https?:|mailto:)", href, re.I):
        return href
    return "https://" + href


def autolink(text):
    """A link for a bare URL or address, as Gmail would make one."""
    t = text.rstrip(".,;:)")
    if not URLISH.match(t):
        return ""
    if "@" in t and "/" not in t:
        return "mailto:" + t
    return full_url(t)


# --------------------------------------------------------------------------- #
# HTML -> blocks of styled runs
# --------------------------------------------------------------------------- #

class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []                    # (kind, depth, marker, runs)
        self.kind, self.marker, self.runs = "p", "", []
        self.stack = [("", {"bold": False, "italic": False, "underline": False,
                            "rgb": None, "href": ""})]
        self.lists = []                     # [tag, items so far]

    def has_text(self):
        return any(t is not BREAK and t.strip() for t, _ in self.runs)

    def flush(self):
        runs, self.runs = self.runs, []
        if any(t is not BREAK and t.strip() for t, _ in runs):
            while runs[-1][0] is BREAK or not runs[-1][0].strip():
                runs.pop()
            self.blocks.append((self.kind, len(self.lists), self.marker, runs))
        elif any(t is BREAK for t, _ in runs):          # <p><br></p> is a blank line
            self.blocks.append((self.kind, len(self.lists), "", []))
        self.marker = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "br":
            self.runs.append((BREAK, self.stack[-1][1]))
            return
        if tag == "hr":
            self.flush()
            self.blocks.append(("hr", 0, "", []))
            return
        if tag in ("ul", "ol"):
            self.flush()
            self.lists.append([tag, 0])
            return
        if tag in BLOCKS:
            if tag in ("p", "div") and self.kind == "li" and not self.has_text():
                return                              # <li><p>…</p></li>
            self.flush()
            if tag == "li":
                if self.lists:
                    self.lists[-1][1] += 1
                kind, n = self.lists[-1] if self.lists else ("ul", 1)
                self.marker = "%d." % n if kind == "ol" else "•"
            self.kind = {"h4": "h3", "h5": "h3", "h6": "h3", "div": "p",
                         "blockquote": "p", "pre": "p"}.get(tag, tag)
            return

        st = dict(self.stack[-1][1])
        if tag in ("b", "strong"):
            st["bold"] = True
        elif tag in ("i", "em"):
            st["italic"] = True
        elif tag == "u":
            st["underline"] = True
        elif tag == "a":
            st["href"] = full_url(a.get("href"))
        elif tag == "font":
            st["rgb"] = parse_color(a.get("color")) or st["rgb"]
        elif tag != "span":
            return
        for decl in (a.get("style") or "").split(";"):
            prop, _, val = decl.partition(":")
            prop, val = prop.strip().lower(), val.strip().lower()
            if prop == "color":
                st["rgb"] = parse_color(val) or st["rgb"]
            elif prop == "font-weight":
                st["bold"] = val in ("bold", "bolder") or val.isdigit() and int(val) >= 600
            elif prop == "font-style":
                st["italic"] = val in ("italic", "oblique")
            elif prop.startswith("text-decoration") and "underline" in val:
                st["underline"] = True
        self.stack.append((tag, st))

    def handle_endtag(self, tag):
        if tag in ("ul", "ol"):
            self.flush()
            if self.lists:
                self.lists.pop()
            self.kind = "p"
        elif tag in BLOCKS:
            self.flush()
            self.kind = "p"
        else:
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    break

    def handle_data(self, data):
        text = re.sub(r"\s+", " ", data)
        if text:
            self.runs.append((text, self.stack[-1][1]))


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #

def _words(runs):
    """Runs -> words (lists of (text, style) fragments), SPACEs and BREAKs."""
    items, joinable = [], False
    for text, st in runs:
        if text is BREAK:
            items.append(BREAK)
            joinable = False
            continue
        for piece in re.findall(r" +|[^ ]+", text):
            if piece[0] == " ":
                if items and items[-1] is not SPACE and items[-1] is not BREAK:
                    items.append(SPACE)
                joinable = False
            elif joinable:
                items[-1].append((piece, st))
            else:
                items.append([(piece, st)])
                joinable = True
    return items


def _lines(items, size, avail, heading):
    def frag_w(text, st):
        return width(encode(text), st["bold"] or heading, size)

    space_w = width(b" ", heading, size)
    lines, line, used = [], [], 0.0
    for item in items:
        if item is BREAK:
            lines.append(line)
            line, used = [], 0.0
            continue
        if item is SPACE:
            if line:
                line.append(SPACE)
                used += space_w
            continue
        w = sum(frag_w(t, st) for t, st in item)
        if line and used + w > avail:
            while line and line[-1] is SPACE:
                line.pop()
            lines.append(line)
            line, used = [], 0.0
        if w > avail:                       # a URL longer than the line: cut it anywhere
            for t, st in item:
                for ch in t:
                    cw = frag_w(ch, st)
                    if line and used + cw > avail:
                        lines.append(line)
                        line, used = [], 0.0
                    line.append([(ch, st)])
                    used += cw
            continue
        line.append(item)
        used += w
    while line and line[-1] is SPACE:
        line.pop()
    lines.append(line)
    return lines


def _pdf_str(data):
    out = bytearray()
    for b in data:
        if b in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([b])
        elif b < 32 or b > 126:
            out += b"\\%03o" % b
        else:
            out.append(b)
    return bytes(out)


class _Pages:
    def __init__(self):
        self.pages = []
        self.new_page()

    def new_page(self):
        self.ops, self.links = [], []
        self.pages.append((self.ops, self.links))
        self.y = PAGE_H - MARGIN

    def at_top(self):
        return self.y >= PAGE_H - MARGIN - 0.01

    def text(self, x, y, size, bold, italic, rgb, data):
        font = 1 + int(bold) + 2 * int(italic)
        self.ops.append(b"%.3f %.3f %.3f rg BT /F%d %.2f Tf %.2f %.2f Td (%s) Tj ET"
                        % (rgb + (font, size, x, y, _pdf_str(data))))

    def rule(self, x, y, w, h, rgb):
        self.ops.append(b"%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f" % (rgb + (x, y, w, h)))


def _layout(blocks):
    doc = _Pages()
    for kind, depth, marker, runs in blocks:
        if kind == "hr":
            if doc.y - 14 < MARGIN:
                doc.new_page()
            doc.rule(MARGIN, doc.y - 7, PAGE_W - 2 * MARGIN, 0.6, (0.8, 0.82, 0.85))
            doc.y -= 14
            continue
        heading = kind in SIZES
        size = SIZES.get(kind, BODY_SIZE)
        indent = depth * LIST_INDENT if kind == "li" else 0
        avail = PAGE_W - 2 * MARGIN - indent
        line_h = size * LEADING
        if heading and not doc.at_top():
            doc.y -= size * 0.35
        for i, line in enumerate(_lines(_words(runs), size, avail, heading)):
            if doc.y - line_h < MARGIN:
                doc.new_page()
            base = doc.y - line_h + (line_h - size) / 2 + size * 0.22
            if i == 0 and marker:
                data = encode(marker)
                doc.text(MARGIN + indent - 5 - width(data, False, size), base, size,
                         False, False, INK, data)
            x = MARGIN + indent
            for item in line:
                if item is SPACE:
                    x += width(b" ", heading, size)
                    continue
                for t, st in item:
                    data = encode(t)
                    bold = st["bold"] or heading
                    w = width(data, bold, size)
                    href = st["href"] or autolink(t)
                    rgb = st["rgb"] or (LINK_RGB if st["href"] else INK)
                    doc.text(x, base, size, bold, st["italic"], rgb, data)
                    if st["underline"]:
                        doc.rule(x, base - size * 0.12, w, max(0.5, size * 0.055), rgb)
                    if href:
                        doc.links.append((x, base - size * 0.25, x + w, base + size * 0.85, href))
                    x += w
            doc.y -= line_h
        doc.y -= {"li": 2.5}.get(kind, 4 if heading else PARA_GAP)
    return doc.pages


# --------------------------------------------------------------------------- #
# PDF file
# --------------------------------------------------------------------------- #

def _assemble(pages, title):
    objs = []

    def add(body):
        objs.append(body)
        return len(objs)

    catalog, root = add(None), add(None)
    fonts = [add(b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
                 % name) for name in (b"Helvetica", b"Helvetica-Bold", b"Helvetica-Oblique",
                                      b"Helvetica-BoldOblique")]
    font_res = b" ".join(b"/F%d %d 0 R" % (i, ref) for i, ref in enumerate(fonts, 1))
    kids = []
    for ops, links in pages:
        stream = zlib.compress(b"\n".join(ops))
        contents = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream)
                       + stream + b"\nendstream")
        annots = [add(b"<< /Type /Annot /Subtype /Link /Rect [%.2f %.2f %.2f %.2f] /Border [0 0 0] "
                      b"/A << /Type /Action /S /URI /URI (%s) >> >>"
                      % (x1, y1, x2, y2, _pdf_str(href.encode("ascii", errors="ignore"))))
                  for x1, y1, x2, y2, href in links]
        kids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources << /Font << %s >> >> "
            b"/Contents %d 0 R%s >>"
            % (root, PAGE_W, PAGE_H, font_res, contents,
               b" /Annots [%s]" % b" ".join(b"%d 0 R" % a for a in annots) if annots else b"")))
    objs[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % root
    objs[root - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    info = add(b"<< /Title (%s) /Producer (job_apply_bot) >>" % _pdf_str(encode(title)))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for num, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, catalog, info, xref))
    return bytes(out)


def render(html_text, title=""):
    parser = _Parser()
    parser.feed(html_text or "")
    parser.close()
    parser.flush()
    return _assemble(_layout(parser.blocks), title)
