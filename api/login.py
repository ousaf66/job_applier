import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _core import Abort, check_login, endpoint


def login(payload):
    token = check_login(payload.get("password"))
    if not token:
        raise Abort("Wrong password")
    return {"token": token}


handler = endpoint(login, auth=False)
