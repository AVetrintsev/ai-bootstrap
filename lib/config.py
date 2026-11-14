"""Validate the operator YAML and stage the profile and firewall."""

import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys

import yaml


INTERNAL_NETWORKS = tuple(
    ipaddress.IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
SANDBOX_UID_MIN = 60001
SANDBOX_UID_MAX = 60999


class UniqueKeysLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate YAML key")
        return super().construct_mapping(node, deep=deep)


def load_mapping(source, field):
    try:
        result = yaml.load(source, Loader=UniqueKeysLoader)
    except (yaml.YAMLError, TypeError, ValueError, RecursionError):
        raise ValueError(f"invalid YAML in {field}") from None
    return mapping(result, field)


def mapping(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a YAML mapping")
    return value


def require(values, key, expected, prefix):
    actual = values.get(key)
    if type(actual) is not type(expected) or actual != expected:
        raise ValueError(f"{prefix}.{key} must be {expected}")


def validate_profile(profile):
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("mihomo.profile must be a nonempty multiline string")
    config = load_mapping(profile, "mihomo.profile")
    require(config, "mode", "rule", "mihomo.profile")
    require(config, "ipv6", False, "mihomo.profile")

    tun = mapping(config.get("tun"), "mihomo.profile.tun")
    for key, expected in (
        ("enable", True),
        ("device", "ai-mihomo"),
        ("auto-route", False),
        ("auto-redirect", False),
    ):
        require(tun, key, expected, "mihomo.profile.tun")
    hijack = tun.get("dns-hijack")
    if not isinstance(hijack, list) or not all(
        target in hijack for target in ("any:53", "tcp://any:53")
    ):
        raise ValueError("mihomo.profile.tun.dns-hijack must cover UDP and TCP port 53")

    dns = mapping(config.get("dns"), "mihomo.profile.dns")
    require(dns, "enable", True, "mihomo.profile.dns")
    require(dns, "listen", "127.0.0.1:1053", "mihomo.profile.dns")
    require(dns, "ipv6", False, "mihomo.profile.dns")
    if not isinstance(dns.get("nameserver"), list) or not dns["nameserver"]:
        raise ValueError("mihomo.profile.dns.nameserver must list at least one resolver")

    rules = config.get("rules")
    if not isinstance(rules, list) or not rules or not isinstance(rules[-1], str) or [
        part.strip() for part in rules[-1].split(",")
    ] not in (["MATCH", "REJECT"], ["MATCH", "DIRECT"]):
        raise ValueError("mihomo.profile.rules must end in MATCH,REJECT or explicit MATCH,DIRECT")


def validate_portal(portal):
    transport = portal.get("transport", "https")
    if transport not in ("local-http", "https"):
        raise ValueError("portal.transport must be local-http or https")
    if transport == "https":
        domain = portal.get("domain")
        label = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
        if (
            not isinstance(domain, str)
            or len(domain) > 253
            or not re.fullmatch(rf"(?:{label}\.)+{label}", domain)
            or domain.rsplit(".", 1)[-1].isdigit()
        ):
            raise ValueError("portal.domain must be a DNS hostname for https")
        address = portal.get("expected_ipv4")
        if not isinstance(address, str):
            raise ValueError("portal.expected_ipv4 must be an IPv4 address for https")
        try:
            ipaddress.IPv4Address(address)
        except ValueError:
            raise ValueError("portal.expected_ipv4 must be an IPv4 address for https") from None
    return transport


def sandbox_internal_tcp(config):
    endpoints = config.get("internal_tcp", [])
    error = "sandbox.internal_tcp must list private IPv4 addresses with TCP ports other than 53 (IP:PORT)"
    if not isinstance(endpoints, list):
        raise ValueError(error)
    rules = set()
    for endpoint in endpoints:
        if not isinstance(endpoint, str) or not re.fullmatch(r"[0-9.]+:[0-9]{1,5}", endpoint):
            raise ValueError(error)
        host, port = endpoint.split(":")
        try:
            address = ipaddress.IPv4Address(host)
        except ValueError:
            raise ValueError(error) from None
        port = int(port)
        if not any(address in network for network in INTERNAL_NETWORKS) or not 1 <= port <= 65535 or port == 53:
            raise ValueError(error)
        rules.add(f"{address} . {port}")
    return f"elements = {{ {', '.join(sorted(rules))} }}" if rules else ""


def stage(config_path, work):
    try:
        fd = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as source:
            mode = os.fstat(source.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise ValueError("config must be a regular file with mode 0600")
            config = load_mapping(source, "config")
    except (OSError, UnicodeError):
        raise ValueError("cannot read config as a regular UTF-8 file") from None

    ssh = mapping(config.get("ssh"), "ssh")
    any_ipv4 = ssh.get("allow_from_any_ipv4", False)
    if type(any_ipv4) is not bool or (any_ipv4 and "admin_ipv4" in ssh):
        raise ValueError("ssh.allow_from_any_ipv4 must be boolean and cannot be combined with admin_ipv4")
    admins = ["0.0.0.0/0"] if any_ipv4 else ssh.get("admin_ipv4")
    if not isinstance(admins, list) or not admins or not all(isinstance(item, str) for item in admins):
        raise ValueError("ssh.admin_ipv4 must list administrator IPv4 addresses or CIDRs")
    try:
        networks = tuple(ipaddress.collapse_addresses(ipaddress.IPv4Network(item) for item in admins))
    except ValueError:
        raise ValueError("ssh.admin_ipv4 contains an invalid IPv4 address or CIDR") from None
    if networks[0].prefixlen == 0 and not any_ipv4:
        raise ValueError("ssh.admin_ipv4 must restrict access to administrator networks")

    transport = validate_portal(mapping(config.get("portal", {}), "portal"))
    profile = mapping(config.get("mihomo"), "mihomo").get("profile")
    validate_profile(profile)
    sandbox_enabled = "sandbox" in config
    sandbox = mapping(config.get("sandbox", {}), "sandbox")
    internal_tcp = sandbox_internal_tcp(sandbox)
    resources = []
    for field in ("memory_percent", "cpu_percent"):
        value = sandbox.get(field, 80)
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError(f"sandbox.{field} must be an integer from 1 to 100")
        resources.append(value)
    gateway = config.get("model_gateway")
    if gateway is not None:
        if not sandbox_enabled:
            raise ValueError("model_gateway requires sandbox")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from model_gateway import validate_settings
        gateway = validate_settings(gateway)

    memory = config.get("project_memory")
    if memory is not None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from memory_config import validate_settings as validate_memory
        memory = validate_memory(memory, gateway)

    template = Path(__file__).resolve().parent / "firewall.nft"
    rules = template.read_text(encoding="utf-8").replace("@ADMIN_IPV4@", ", ".join(map(str, networks)))
    rules = rules.replace("@SANDBOX_UID_RANGE@", f"{SANDBOX_UID_MIN}-{SANDBOX_UID_MAX}")
    rules = rules.replace("@SANDBOX_INTERNAL_TCP@", internal_tcp)
    gateway_rule = "fib daddr type local tcp dport 18081 accept" if gateway is not None else ""
    rules = rules.replace("@MODEL_GATEWAY_ROUTE@", gateway_rule).replace("@MODEL_GATEWAY_EGRESS@", gateway_rule)
    rules = rules.replace(
        "@SANDBOX_ROUTE@", "jump sandbox_route" if sandbox_enabled else "drop"
    ).replace(
        "@SANDBOX_MIHOMO_GUARD@",
        'meta skuid "ai-mihomo" meta nfproto ipv4 ct direction original jump mihomo_dial'
        if sandbox_enabled else "",
    )
    (work / "firewall.nft").write_text(rules, encoding="utf-8")
    (work / "admin-ipv4").write_text("\n".join(map(str, networks)), encoding="utf-8")
    (work / "profile.yaml").write_text(profile, encoding="utf-8")
    (work / "portal-transport").write_text(transport + "\n", encoding="utf-8")
    (work / "model-gateway.json").write_text(json.dumps(gateway) + "\n", encoding="utf-8")
    (work / "project-memory.json").write_text(json.dumps(memory) + "\n", encoding="utf-8")
    enabled = "yes" if sandbox_enabled else "no"
    (work / "sandbox-settings").write_text(
        f"{enabled} {SANDBOX_UID_MIN} {SANDBOX_UID_MAX}\n", encoding="utf-8"
    )
    (work / "sandbox-resources").write_text(" ".join(map(str, resources)) + "\n", encoding="utf-8")


def check_ssh(work):
    networks = [ipaddress.IPv4Network(line) for line in (work / "admin-ipv4").read_text().splitlines()]
    for connection in sys.stdin:
        peer = connection.split()[3].rsplit(":", 1)[0].strip("[]")
        address = ipaddress.ip_address(peer)
        if isinstance(address, ipaddress.IPv6Address):
            address = address.ipv4_mapped
        if address is None or not any(address in network for network in networks):
            raise ValueError("an active SSH peer is outside ssh.admin_ipv4")


if __name__ == "__main__":
    try:
        if sys.argv[1] == "--check-ssh":
            check_ssh(Path(sys.argv[2]))
        else:
            stage(sys.argv[1], Path(sys.argv[2]))
    except (OSError, ValueError, IndexError) as error:
        message = str(error) if isinstance(error, ValueError) else "could not stage or check configuration"
        sys.exit(message)
