"""Logging: Konsole (INFO) + Datei (DEBUG) pro Lauf."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path


def setup_logging(logs_dir: Path, command: str, verbose: bool = False) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{command}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"))
    root.addHandler(fh)

    # Konsole robust gegen nicht-UTF-8-Terminals
    try:
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
        else:  # Umleitung/Pipe (z. B. Git Bash): UTF-8 erzwingen
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(ch)

    # Drittbibliotheken leiser
    for noisy in ("PIL", "exiftool", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file
