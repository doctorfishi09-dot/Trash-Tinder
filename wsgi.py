"""PythonAnywhere WSGI entry point.

PythonAnywhere's "Web" tab has a WSGI configuration file (something like
/var/www/<username>_pythonanywhere_com_wsgi.py) that must expose a module-
level `application` callable. Paste the contents of *this* file into that
configuration file — adjusting PROJECT_DIR to the path where you uploaded
the project — and the app will serve from your *.pythonanywhere.com URL.

You can also import from this file directly if your PA WSGI config can
reach it (e.g. `from wsgi import application`).
"""

import os
import sys

# Change this to the absolute path of the project directory on the server.
# On PythonAnywhere it will look something like:
#   /home/<your-username>/trash-tinder
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from server import application  # noqa: E402,F401
