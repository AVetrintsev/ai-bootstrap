from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from github_projects import GitHubApp, ProjectError, Projects, credential


class GitHubProjectsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        (self.state / "github-app.pem").write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        (self.state / "github-app-id").write_text("31\n", encoding="ascii")
        self.tokens = []
        self.repository_selection = "selected"
        fake_api = patch("github_projects.request", side_effect=self.github)
        fake_api.start()
        self.addCleanup(fake_api.stop)

    def github(self, method, path, token, body=None):
        if method == "GET" and path == "/app":
            return {"id": 31, "slug": "bootstrap-test", "owner": {"id": 6, "login": "owner"}}
        if method == "GET" and path == "/app/installations?per_page=2":
            return [{"id": 52, "suspended_at": None, "account": {"id": 6},
                     "repository_selection": self.repository_selection}]
        if method == "GET" and path == "/app/installations/52":
            return {"permissions": {"contents": "write", "issues": "write"}}
        if method == "POST" and path == "/app/installations/52/access_tokens":
            if body == {"permissions": {"metadata": "read"}}:
                return {"token": "listing-token"}
            self.tokens.append(body)
            return {"token": f"scoped-{len(self.tokens)}"}
        if method == "GET" and path == "/installation/repositories?per_page=100&page=1":
            self.assertEqual(token, "listing-token")
            return {"repositories": [
                {"id": 7, "full_name": "owner/first", "private": True},
                {"id": 8, "full_name": "owner/second", "private": True},
                {"id": 9, "full_name": "owner/public", "private": False},
            ]}
        raise AssertionError((method, path))

    def app(self):
        return GitHubApp(self.state)

    def open_portal(self, sandbox):
        projects = Projects(self.state, self.app, sandbox)
        self.addCleanup(projects.jobs.shutdown)
        return projects

    def wait_for(self, projects, expected):
        for _ in range(200):
            rows = projects.list()["projects"]
            snapshot = {row["id"]: row["state"] for row in rows}
            if snapshot == expected and all(row.get("canvas") != "connecting" for row in rows):
                return
            time.sleep(0.01)
        self.assertEqual(snapshot, expected)

    def test_all_repositories_installation_still_issues_credentials_for_one_project(self):
        self.repository_selection = "all"
        projects = self.open_portal(lambda *_: None)
        self.assertEqual(self.app().setup_page(), {
            "install_url": "https://github.com/apps/bootstrap-test/installations/new", "account": "owner",
        })
        self.assertFalse(projects.list()["connected"])
        projects.connect()
        self.assertEqual(projects.repositories()["repositories"], [
            {"id": 7, "name": "owner/first"},
            {"id": 8, "name": "owner/second"},
        ])
        with self.assertRaises(ProjectError):
            projects.add(9)
        projects.add(7)
        projects.add(8)
        self.wait_for(projects, {7: "ready", 8: "ready"})

        self.assertEqual(credential(self.state, self.app, "aisbx-gh-7", "owner/second.git"), {})
        self.assertEqual(credential(self.state, self.app, "aisbx-gh-8", "owner/first.git"), {})
        self.assertEqual(credential(self.state, self.app, "owner", "owner/first.git"), {})
        self.assertEqual(self.tokens, [])
        self.assertEqual(credential(self.state, self.app, "aisbx-gh-7", "owner/first.git"), {"token": "scoped-1"})
        self.assertEqual(credential(self.state, self.app, "aisbx-gh-8", "owner/second.git"), {"token": "scoped-2"})
        self.assertEqual(credential(self.state, self.app, "aisbx-gh-7", "owner/first.git"), {"token": "scoped-3"})
        self.assertEqual(self.tokens, [
            {"repository_ids": [7], "permissions": {"metadata": "read", "contents": "write"}},
            {"repository_ids": [8], "permissions": {"metadata": "read", "contents": "write"}},
            {"repository_ids": [7], "permissions": {"metadata": "read", "contents": "write"}},
        ])

    def test_failed_add_reuses_the_sandbox_and_only_explicit_delete_removes_it(self):
        first_attempt = Event()
        attempts = []

        def sandbox(action, project, repository):
            attempts.append((action, project, repository))
            if not first_attempt.is_set():
                first_attempt.set()
                raise ProjectError("clone failed")

        projects = self.open_portal(sandbox)
        projects.connect()
        projects.add(7)
        self.wait_for(projects, {7: "failed"})
        projects = self.open_portal(sandbox)
        projects.add(7)
        self.wait_for(projects, {7: "ready"})
        projects.add(7)
        self.assertEqual([attempt for attempt in attempts if attempt[0] == "create"],
                         [("create", "gh-7", "owner/first")] * 2)
        projects.delete(7)
        self.wait_for(projects, {})
        self.assertEqual(attempts[-1], ("delete", "gh-7", None))
        self.assertEqual(credential(self.state, self.app, "aisbx-gh-7", "owner/first.git"), {})

    def test_canvas_failure_keeps_checkout_and_retry_connects_the_same_project(self):
        fail_connection = True
        sandboxes = set()

        def sandbox(action, project, repository=None):
            nonlocal fail_connection
            if action == "create":
                sandboxes.add(project)
            if action == "canvas" and fail_connection:
                raise ProjectError("agent server unavailable")

        projects = self.open_portal(sandbox)
        projects.connect()
        projects.add(7)
        for _ in range(200):
            rows = projects.list()["projects"]
            if rows and rows[0].get("canvas") == "failed":
                break
            time.sleep(0.01)
        self.assertEqual(rows[0]["state"], "ready")
        self.assertEqual(rows[0].get("canvas"), "failed")
        self.assertEqual(credential(self.state, self.app, "aisbx-gh-7", "owner/first.git"), {"token": "scoped-1"})
        fail_connection = False
        projects.connect_canvas(7)
        for _ in range(200):
            row = projects.list()["projects"][0]
            if row.get("canvas") == "ready":
                break
            time.sleep(0.01)
        self.assertEqual(row["canvas"], "ready")
        self.assertEqual(sandboxes, {"gh-7"})
        self.assertEqual(len(projects.list()["projects"]), 1)

    def test_project_waits_for_index_and_retry_keeps_its_sandbox(self):
        indexing = Event()
        finish = Event()
        self.addCleanup(finish.set)
        sandboxes = set()
        fail_index = True

        def sandbox(action, project, repository=None):
            if action == 'create':
                sandboxes.add(project)
            if action == 'tools':
                indexing.set()
                if not finish.wait(3):
                    raise ProjectError('index timed out')
                if fail_index:
                    raise ProjectError('index failed')

        projects = self.open_portal(sandbox)
        projects.connect()
        projects.add(7)
        self.assertTrue(indexing.wait(1))
        self.assertEqual(projects.list()['projects'][0]['state'], 'creating')
        finish.set()
        self.wait_for(projects, {7: 'failed'})
        fail_index = False
        projects.add(7)
        self.wait_for(projects, {7: 'ready'})
        self.assertEqual(sandboxes, {'gh-7'})
        self.assertEqual(len(projects.list()['projects']), 1)
