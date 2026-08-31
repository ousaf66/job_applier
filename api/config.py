import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _core import endpoint, put_config, put_resume, resume_info


def save(payload):
    out = {"ok": True}
    if payload.get("resume_b64"):
        out["resume_bytes"] = put_resume(payload["resume_b64"], payload.get("resume_name"))
    put_config(payload)
    out["resume"] = resume_info()
    return out


handler = endpoint(save)
