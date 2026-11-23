import os
import sys
import tempfile
import atexit
import logging

# Make the server modules importable and keep the server's logs and config out of the user's home.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_home = tempfile.TemporaryDirectory(prefix="handy-remote-test-")
os.environ["HANDY_REMOTE_HOME"] = _home.name


def _cleanup():
    logging.shutdown()
    _home.cleanup()


atexit.register(_cleanup)
