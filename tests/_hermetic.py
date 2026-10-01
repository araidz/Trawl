"""Import this first in every test: point HOME at a throwaway folder before `trawl` is imported.

trawl's state folder (config, history, downloads log, subscriptions…) is derived from HOME when
the package is imported, so tests would otherwise read and overwrite the real ones.
"""

import atexit
import os
import shutil
import tempfile

HOME = tempfile.mkdtemp(prefix="trawl-test-home-")
os.environ["HOME"] = HOME
atexit.register(shutil.rmtree, HOME, ignore_errors=True)
