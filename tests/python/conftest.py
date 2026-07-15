"""Test scaffolding: make app/main.py importable without /config write access."""
import os
import sys
import tempfile

_TEST_CONFIG = tempfile.mkdtemp(prefix="mangarr-test-config-")

# Redirect any os.makedirs('/config...') to the tmp dir so module import succeeds.
def _redir(path):
    if isinstance(path, str) and path.startswith("/config"):
        return path.replace("/config", _TEST_CONFIG, 1)
    return path

_real_makedirs = os.makedirs
def _redir_makedirs(path, *a, **kw):
    return _real_makedirs(_redir(path), *a, **kw)
os.makedirs = _redir_makedirs

_real_isdir = os.path.isdir
def _redir_isdir(path):
    return _real_isdir(_redir(path))
os.path.isdir = _redir_isdir

# Pre-create the covers dir so StaticFiles(directory="/config/covers") passes its check.
_real_makedirs(os.path.join(_TEST_CONFIG, "covers"), exist_ok=True)

# StaticFiles validates `directory` exists at import time. In tests we don't
# serve static files; bypass the check rather than create /app/static globally.
import starlette.staticfiles as _sf  # noqa: E402
_orig_sf_init = _sf.StaticFiles.__init__
def _patched_sf_init(self, *args, **kw):
    kw["check_dir"] = False
    return _orig_sf_init(self, *args, **kw)
_sf.StaticFiles.__init__ = _patched_sf_init

# Redirect sqlite3 connections at /config to the tmp dir.
import sqlite3 as _sqlite3  # noqa: E402
_real_connect = _sqlite3.connect
def _redir_connect(database, *a, **kw):
    if isinstance(database, str) and database.startswith("/config"):
        database = database.replace("/config", _TEST_CONFIG, 1)
    return _real_connect(database, *a, **kw)
_sqlite3.connect = _redir_connect

# app/ on sys.path so `import main` works
APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "app"))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Existing route tests exercise application behavior rather than browser auth.
# Use the internal-only test switch so they do not all need session setup;
# dedicated auth tests turn this back off and browser suites log in normally.
os.environ["MANGARR_CONFIG_DIR"] = _TEST_CONFIG
import auth as _auth  # noqa: E402
_auth.set_test_auth_bypass(True)

# Redirect Jinja2 template loading from /app/templates (container path) to
# the host path so integration tests that render templates work outside
# Docker. Only affects the default FileSystemLoader used by Jinja2Templates.
HOST_TEMPLATE_DIR = os.path.join(APP_DIR, "templates")
if os.path.isdir(HOST_TEMPLATE_DIR):
    import jinja2 as _jinja2  # noqa: E402
    _orig_fsl_init = _jinja2.FileSystemLoader.__init__
    def _patched_fsl_init(self, searchpath, *a, **kw):
        if isinstance(searchpath, str) and searchpath == "/app/templates":
            searchpath = HOST_TEMPLATE_DIR
        elif isinstance(searchpath, (list, tuple)):
            searchpath = [HOST_TEMPLATE_DIR if p == "/app/templates" else p
                          for p in searchpath]
        return _orig_fsl_init(self, searchpath, *a, **kw)
    _jinja2.FileSystemLoader.__init__ = _patched_fsl_init
