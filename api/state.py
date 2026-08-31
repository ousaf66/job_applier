import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _core import endpoint, state_payload

handler = endpoint(lambda payload: state_payload(), methods=("GET",))
