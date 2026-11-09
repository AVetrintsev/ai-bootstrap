#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 4 ]]; then
    echo "usage: sudo bash tests/accept_github_projects.sh SANDBOX OWNER/REPO OTHER_SANDBOX OWNER/OTHER_REPO" >&2
    exit 2
fi

check_project() {
    ai-sandbox shell "$1" /usr/bin/python3 - "$2" "$3" <<'PYTHON'
from http.client import HTTPSConnection
import os
import subprocess
import sys

own, other = sys.argv[1:]
os.chdir("/workspace/repo")
env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
expected_url = f"https://github.com/{own}.git"
origin = subprocess.check_output(["git", "remote", "get-url", "origin"], text=True).strip()
if origin != expected_url:
    sys.exit("Unexpected origin in the project checkout")
subprocess.run(["git", "ls-remote", "origin"], env=env, stdout=subprocess.DEVNULL, check=True)
credentials = subprocess.check_output(
    ["git", "credential", "fill"],
    input=f"protocol=https\nhost=github.com\npath={own}.git\n\n", env=env, text=True,
)
token = next(line.removeprefix("password=") for line in credentials.splitlines() if line.startswith("password="))

def github_status(repository):
    connection = HTTPSConnection("api.github.com", timeout=15)
    try:
        connection.request("GET", f"/repos/{repository}", headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "ai-bootstrap-acceptance",
            "Accept": "application/vnd.github+json",
        })
        return connection.getresponse().status
    finally:
        connection.close()

own_status, other_status = github_status(own), github_status(other)
if own_status != 200 or other_status not in (403, 404):
    sys.exit(f"Unexpected token scope: own status {own_status}, other status {other_status}")
print(f"{own}: checkout and Git access verified; token denied access to {other}")
PYTHON
}

[[ $1 != "$3" && $2 != "$4" ]]
check_project "$1" "$2" "$4"
check_project "$3" "$4" "$2"
