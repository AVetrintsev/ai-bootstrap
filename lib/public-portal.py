"""Check DNS and the externally visible portal before publishing it."""

import argparse
import json
import re
import subprocess
import time


GLOBALPING = "https://api.globalping.io/v1/measurements"
DNS = "https://cloudflare-dns.com/dns-query"


def fetch_json(url, *options):
    response = subprocess.run(
        ["/usr/bin/curl", "-q", "-4", "--noproxy", "*", "--fail", "--silent",
         "--compressed", "--connect-timeout", "5", "--max-time", "10",
         "--user-agent", "ai-bootstrap (public portal preflight)", *options, url],
        check=True, stdout=subprocess.PIPE,
    )
    return json.loads(response.stdout)


def verify_dns(domain, expected_ipv4):
    def addresses(kind, code):
        try:
            response = fetch_json(
                f"{DNS}?name={domain}&type={kind}", "--header", "Accept: application/dns-json",
            )
            if response["Status"] != 0 or response.get("TC"):
                raise ValueError
            return {record["data"] for record in response.get("Answer", []) if record["type"] == code}
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError, AttributeError):
            raise SystemExit(f"public DNS {kind} lookup failed through mihomo for {domain}") from None

    ipv4 = addresses("A", 1)
    if ipv4 != {expected_ipv4}:
        observed = ", ".join(sorted(ipv4)) or "no address"
        raise SystemExit(f"public DNS A for {domain}: expected only {expected_ipv4}, got {observed}")
    if addresses("AAAA", 28):
        raise SystemExit(f"unexpected public DNS AAAA for {domain}; remove it for this IPv4 portal")


def globalping_result(domain, path):
    measurement = {
        "type": "http",
        "target": domain,
        "limit": 1,
        "measurementOptions": {
            "protocol": "HTTPS", "port": 443, "ipVersion": 4,
            "request": {"method": "GET", "path": path},
        },
    }
    unavailable = "Globalping unavailable through mihomo or no external probe could run"
    try:
        identifier = fetch_json(
            GLOBALPING, "--header", "Content-Type: application/json", "--data", json.dumps(measurement),
        )["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
            raise ValueError
        url = f"{GLOBALPING}/{identifier}"
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            report = fetch_json(url)
            if report["status"] != "in-progress":
                return report["results"][0]["result"]
            time.sleep(0.5)
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, IndexError, TypeError):
        raise SystemExit(unavailable) from None
    raise SystemExit(unavailable)


def verify_https(domain, expected_ipv4, path, body):
    result = globalping_result(domain, path)
    status = result.get("status")
    if status != "finished":
        source = result.get("failureSource")
        detail = result.get("rawOutput", "").lower()
        if status == "offline" or source == "internal":
            raise SystemExit("Globalping external probe unavailable")
        if source == "resolver" or re.search(r"enotfound|eai_again|getaddrinfo", detail):
            raise SystemExit("public DNS lookup failed at the Globalping probe")
        if re.search(r"certificate|tls|ssl|self.signed", detail):
            raise SystemExit("public TLS handshake or certificate validation failed")
        raise SystemExit("public inbound TCP 443 or HTTPS response failed at the Globalping probe")
    address = result.get("resolvedAddress")
    if address != expected_ipv4:
        raise SystemExit(f"public DNS from Globalping: expected {expected_ipv4}, got {address}")
    if (result.get("tls") or {}).get("authorized") is not True:
        raise SystemExit("public TLS certificate was not trusted by the Globalping probe")
    if result.get("statusCode") != 200 or result.get("truncated") or result.get("rawBody") != body:
        raise SystemExit("public inbound HTTPS did not return the expected status 200 and probe body")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    dns = commands.add_parser("dns")
    probe = commands.add_parser("probe")
    for command in (dns, probe):
        command.add_argument("domain")
        command.add_argument("expected_ipv4")
    probe.add_argument("path")
    probe.add_argument("body")
    args = parser.parse_args()
    if args.command == "dns":
        verify_dns(args.domain, args.expected_ipv4)
    else:
        verify_https(args.domain, args.expected_ipv4, args.path, args.body)
