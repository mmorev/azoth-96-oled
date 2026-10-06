"""The log: a millisecond-stamped stdout line + an optional rotating file mirror."""
from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_MAX_BYTES = 2 * 1024 * 1024           # the log file rotation: ~2 MB
LOG_BACKUPS = 2                           # 3 files in total: azoth-companion.log, .1, .2

_FILE_LOG = None      # the file mirror of the log (a Logger, created by setup_log_file)


def log(msg: str) -> None:
    t = time.time()
    line = (time.strftime("[%H:%M:%S.", time.localtime(t))
            + "%03d] " % (int(t * 1000) % 1000) + msg)
    print(line, flush=True)   # under pythonw stdout=None — print silently skips
    if _FILE_LOG is not None:
        _FILE_LOG.info(line)


def setup_log_file(path: str) -> None:
    """A file mirror of the log: the same lines as stdout (not a redirection!),
    UTF-8, size rotation (the standard RotatingFileHandler). An empty string
    = the --log-file flag without a value: logs/azoth-companion.log next to main.py
    (the directory is created). A relative PATH is resolved from the CWD."""
    global _FILE_LOG
    if not path:
        path = str(Path(__file__).resolve().parent.parent / "logs" / "azoth-companion.log")
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(str(p), maxBytes=LOG_MAX_BYTES,
                                      backupCount=LOG_BACKUPS, encoding="utf-8")
    except OSError as e:
        print("the log file %s is unavailable (%s) — writing to stdout only" % (p, e), flush=True)
        return
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("azoth-companion.file")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(handler)
    _FILE_LOG = lg
    log("log file: %s (rotation %.0f MB × %d files)"
        % (p, LOG_MAX_BYTES / 1048576, LOG_BACKUPS + 1))
