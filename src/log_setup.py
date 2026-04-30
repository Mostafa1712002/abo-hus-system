"""Shared logging configuration for ad-hoc scripts.

This module provides a single :func:`setup_logging` function used by the
helper scripts that live at the project root (``upload_to_cold*.py``,
``transcode_rmvb.py``, the ``sync_*.py`` scripts, etc.) so that every
operation we run leaves a paper trail under ``logs/`` — independent of how
the script was launched (cron, systemd, manual run from a Windows shell).

Design choices:

- One log file per *category* (``cold_upload.log``, ``transcode.log``,
  ``sync_ops.log``, ``setup.log``). Multiple invocations append rather than
  truncate so the file is a long-running history.
- Console handler stays on ``stdout`` so existing ``tqdm`` progress bars
  (which write to ``stderr`` by default) keep rendering correctly without
  fighting the logger for the same stream.
- File output is UTF-8 because every script in this project emits Arabic
  filenames at some point.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(
    log_filename: str,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> logging.Logger:
    """Configure logging to BOTH console and a file under ``logs/``.

    Parameters
    ----------
    log_filename:
        Bare file name (e.g. ``"cold_upload.log"``). The file is created
        under ``<project_root>/logs/`` and opened in append mode.
    console_level / file_level:
        Standard ``logging`` levels. The file is verbose by default so we
        can post-mortem; the console stays at ``INFO`` to avoid drowning
        the operator in details.

    Returns
    -------
    logging.Logger
        The root logger, so callers can also do
        ``log = logging.getLogger(__name__)`` after calling us.
    """
    project_root = Path(__file__).resolve().parent.parent
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_filename

    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger()
    # Idempotent: if the caller invokes setup_logging twice we don't want
    # duplicate handlers stacking up and double-printing every record.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)

    # File handler — UTF-8 for Arabic filenames and Gemini outputs.
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(file_level)
    fh.setFormatter(file_fmt)
    root.addHandler(fh)

    # Console handler — stdout (tqdm uses stderr, so they don't fight).
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(console_level)
    ch.setFormatter(console_fmt)
    root.addHandler(ch)

    root.info("=== logging initialized -> %s ===", log_path)
    return root
