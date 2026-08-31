import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _core import endpoint, put_jobs


def save(payload):
    return {"ok": True, "saved": put_jobs(payload.get("rows") or [])}


handler = endpoint(save)
