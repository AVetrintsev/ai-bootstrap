"""Run the fixed sandbox lifecycle commands for the GitHub service."""

import json
import pwd
import re
import socket
import struct
import subprocess
import sys


def provision():
    with socket.fromfd(0, socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != pwd.getpwnam("ai-github").pw_uid:
            return {}
        connection.settimeout(10)
        with connection.makefile("rb") as stream:
            line = stream.readline(2048)
    if not line.endswith(b"\n"):
        return {}
    data = json.loads(line)
    if not isinstance(data, dict):
        return {}
    action, sandbox, repository = data.get("action"), data.get("sandbox"), data.get("repository")
    if not isinstance(sandbox, str) or not re.fullmatch(r"gh-[1-9][0-9]{0,18}", sandbox):
        return {}
    command = ["/usr/local/sbin/ai-sandbox"]
    if action == "create" and isinstance(repository, str):
        # ai-sandbox independently validates the repository name before making changes.
        command += ["github-create", sandbox, repository]
    elif action == "delete" and repository is None:
        command += ["github-delete", sandbox]
    else:
        return {}
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=880)
    return {"ok": result.returncode == 0}


if __name__ == "__main__":
    try:
        result = provision()
    except (OSError, KeyError, ValueError, subprocess.TimeoutExpired):
        result = {}
    print(json.dumps(result), flush=True)
