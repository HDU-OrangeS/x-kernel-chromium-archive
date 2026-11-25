#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 KylinSoft Co., Ltd. <https://www.kylinos.cn/>
# See LICENSES for license details.
"""Small host/guest filesystem guards; no VM or process launch side effects."""
import errno
import os
from pathlib import Path
import socket
import stat


def stale_socket(path, remove=False):
    """Recognize only owned Unix sockets whose connect explicitly refuses.

    Missing paths are harmless. Live listeners, permission/time-out failures,
    symlinks, regular files and replacement races are never removed. Callers
    first check exclusive process ownership and the containing directory.
    """
    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.geteuid():
        raise RuntimeError('Refuse non-socket, symlink or foreign socket: ' + str(path))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(.5)
        try:
            client.connect(str(path))
        except OSError as error:
            if error.errno != errno.ECONNREFUSED:
                raise RuntimeError('Socket state is uncertain; retained: ' + str(path)) from error
        else:
            raise RuntimeError('Socket has a live listener; retained: ' + str(path))
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_uid, before.st_mode) != (
            after.st_dev, after.st_ino, after.st_uid, after.st_mode):
        raise RuntimeError('Socket changed during inspection; retained: ' + str(path))
    if remove:
        path.unlink()
    return True
