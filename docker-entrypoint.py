#!/usr/bin/env python3
"""Prepare the persistent volume, then run the app without root privileges."""

from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path


APP_USER = "habits"
DATA_DIR = Path("/app/data")


def prepare_data_dir() -> None:
    """Make pre-existing Docker/Portainer volumes writable by the app user."""
    account = pwd.getpwnam(APP_USER)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for root, directories, files in os.walk(DATA_DIR):
        for name in [".", *directories, *files]:
            path = Path(root) if name == "." else Path(root) / name
            try:
                os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
            except FileNotFoundError:
                # A temporary SQLite WAL file may disappear during a restart.
                continue


def drop_privileges() -> None:
    account = pwd.getpwnam(APP_USER)
    os.setgroups([])
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)


def main() -> None:
    if os.geteuid() == 0:
        prepare_data_dir()
        drop_privileges()
    if not sys.argv[1:]:
        raise SystemExit("Brak komendy do uruchomienia")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
