"""Resolve the host IPv4 used by both terminal Qwen and Canvas."""
import ipaddress
import json
import subprocess

routes = json.loads(subprocess.check_output(["ip", "-4", "-j", "route", "show", "default"]))
for route in routes:
    interfaces = json.loads(subprocess.check_output(["ip", "-4", "-j", "address", "show", "dev", route["dev"]]))
    for interface in interfaces:
        for address in interface["addr_info"]:
            if address["scope"] == "global":
                print(ipaddress.IPv4Address(address["local"]))
                raise SystemExit
raise SystemExit("No host IPv4 address for the model gateway")
