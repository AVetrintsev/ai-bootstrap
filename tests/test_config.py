import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def stage(self, content, mode=0o600, artifact="profile.yaml"):
        with tempfile.TemporaryDirectory() as directory:
            work = pathlib.Path(directory)
            config = work / "config.yaml"
            config.write_text(content, encoding="utf-8")
            config.chmod(mode)
            result = subprocess.run(
                [sys.executable, ROOT / "lib/config.py", config, work],
                capture_output=True,
                text=True,
            )
            output = work / artifact
            return result, output.read_text(encoding="utf-8") if output.exists() else None

    def test_stages_local_transport_without_public_address(self):
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        result, transport = self.stage(example, artifact="portal-transport")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(transport, "local-http\n")

    def test_stages_the_complete_profile_from_the_example(self):
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        result, profile = self.stage(example)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = "".join(line[4:] for line in example.split("  profile: |\n")[1].splitlines(True))
        self.assertEqual(profile, expected)

    def test_rejects_unsafe_config_permissions(self):
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        result, profile = self.stage(example, 0o644)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0600", result.stderr)
        self.assertIsNone(profile)

    def test_rejects_profile_that_enables_competing_routes(self):
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        result, profile = self.stage(example.replace("auto-route: false", "auto-route: true"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tun.auto-route", result.stderr)
        self.assertIsNone(profile)

    def test_yaml_errors_do_not_expose_input_values(self):
        result, profile = self.stage("mihomo: [secret-value,\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("YAML", result.stderr)
        self.assertNotIn("secret-value", result.stderr + result.stdout)
        self.assertIsNone(profile)

    def test_requires_an_explicit_policy_for_unmatched_traffic(self):
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        result, profile = self.stage(example.replace("      - MATCH,REJECT\n", ""))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MATCH,REJECT", result.stderr)
        self.assertIsNone(profile)

    def test_stages_only_explicit_internal_tcp_endpoints(self):
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        result, firewall = self.stage(
            example.replace("internal_tcp: []", "internal_tcp: ['10.2.3.4:8443']"), artifact="firewall.nft"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("elements = { 10.2.3.4 . 8443 }", firewall)

    def test_rejects_public_or_metadata_addresses_in_the_internal_allowlist(self):
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        for endpoint in ("1.1.1.1:443", "169.254.169.254:80", "10.0.0.1:65536", "10.0.0.1:53"):
            with self.subTest(endpoint=endpoint):
                result, firewall = self.stage(
                    example.replace("internal_tcp: []", f"internal_tcp: ['{endpoint}']"), artifact="firewall.nft"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("sandbox.internal_tcp", result.stderr)
                self.assertIsNone(firewall)

    def test_existing_config_keeps_its_mihomo_routes_until_sandboxes_are_enabled(self):
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        existing = example.split("sandbox:\n")[0] + "mihomo:\n" + example.split("mihomo:\n")[1]
        result, firewall = self.stage(existing, artifact="firewall.nft")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("jump mihomo_dial", firewall)
        self.assertIn("meta skuid 60001-60999 drop", firewall)


if __name__ == "__main__":
    unittest.main()
