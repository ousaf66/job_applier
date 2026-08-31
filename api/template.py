import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _core import endpoint, get_template_text, put_template_text, split_template


def read_or_write(payload):
    name = payload.get("name") or "application.txt"
    text = payload.get("text")
    if text is None:
        return {"name": name, "text": get_template_text(name)}
    split_template(text)                     # refuse to save something unsendable
    put_template_text(name, text)
    return {"ok": True}


handler = endpoint(read_or_write, methods=("GET", "POST"))
