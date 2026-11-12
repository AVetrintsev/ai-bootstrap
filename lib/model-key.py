#!/usr/bin/python3 -I
"""Replace a provider key without exposing it in shell history or changing sandbox keys."""
import argparse
import getpass
import grp
import json
import os
from pathlib import Path
import re
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider")
    parser.add_argument("--stdin", action="store_true", help="read the new key from standard input")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run with sudo")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", args.provider):
        parser.error("invalid provider name")
    directory = Path("/etc/ai-bootstrap/model-keys")
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    group = grp.getgrnam("ai-models").gr_gid
    os.chown(directory, 0, group)
    directory.chmod(0o750)
    target = directory / (args.provider + ".key")
    config = Path("/etc/ai-bootstrap/model-gateway.json")
    if config.exists():
        provider = json.loads(config.read_text())["providers"].get(args.provider)
        if provider is not None and Path(provider["key_file"]) != target:
            parser.error("the configured key_file must use /etc/ai-bootstrap/model-keys/PROVIDER.key")
    key = sys.stdin.read(8193).strip() if args.stdin else getpass.getpass("New provider API key: ")
    if not key or len(key) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        parser.error("expected a nonempty API key without whitespace")
    descriptor, staged = tempfile.mkstemp(dir=directory, prefix=".key-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o640)
            os.fchown(stream.fileno(), 0, group)
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, target)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    print("Provider key updated. Subsequent requests use the new key.")


if __name__ == "__main__":
    main()
