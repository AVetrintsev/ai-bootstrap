import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def stage(self, content, mode=0o600):
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
            profile = work / "profile.yaml"
            return result, profile.read_text(encoding="utf-8") if profile.exists() else None

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


if __name__ == "__main__":
    unittest.main()
