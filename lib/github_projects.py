"""GitHub App connection, project lifecycle, and per-sandbox Git credentials."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPSConnection, HTTPException
import json
import os
from pathlib import Path
import pwd
import re
import socket
import sqlite3
import struct
import sys
from threading import BoundedSemaphore, Lock, Thread
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


STATE = Path("/var/lib/ai-github")
CONFIG = Path("/etc/ai-bootstrap")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
SANDBOX = re.compile(r"gh-[1-9][0-9]{0,18}")
UNAVAILABLE = "GitHub недоступен или отклонил запрос."
INVALID_PROJECT = "Проект недоступен."
LIMIT = 2 * 1024 * 1024


class ProjectError(Exception):
    pass


def numeric_id(value):
    return type(value) is int and 0 < value < 2**63


def is_repository_name(value):
    return (isinstance(value, str) and REPOSITORY.fullmatch(value)
            and value.split("/")[1] not in (".", ".."))


def encoded(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def request(method, path, token, body=None):
    with closing(HTTPSConnection("api.github.com", timeout=7)) as connection:
        connection.request(method, path, json.dumps(body) if body is not None else None, {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ai-bootstrap",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise ProjectError(UNAVAILABLE)
        data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ProjectError(UNAVAILABLE)
        return json.loads(data)


class GitHubApp:
    def __init__(self, config):
        try:
            self.id = int((config / "github-app-id").read_text(encoding="ascii").strip())
            self.key = serialization.load_pem_private_key((config / "github-app.pem").read_bytes(), None)
            if not numeric_id(self.id) or not isinstance(self.key, rsa.RSAPrivateKey):
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise ProjectError("Администратору нужно настроить GitHub App на VM.") from None

    def jwt(self):
        now = int(time.time())
        claims = json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(self.id)}).encode()
        payload = encoded(b'{"alg":"RS256","typ":"JWT"}') + b"." + encoded(claims)
        signature = self.key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
        return (payload + b"." + encoded(signature)).decode("ascii")

    def details(self):
        app = request("GET", "/app", self.jwt())
        if app["id"] != self.id or app.get("public") is True:
            raise ProjectError("Настройте выделенное приватное GitHub App.")
        return app

    def setup_page(self):
        app = self.details()
        slug = app["slug"]
        if not re.fullmatch(r"[a-z0-9-]{1,100}", slug):
            raise ProjectError(UNAVAILABLE)
        return {"install_url": f"https://github.com/apps/{slug}/installations/new",
                "account": app["owner"]["login"]}

    def installation(self):
        owner = self.details()["owner"]["id"]
        installations = request("GET", "/app/installations?per_page=2", self.jwt())
        if len(installations) != 1 or installations[0]["account"]["id"] != owner:
            raise ProjectError("Установите выделенное GitHub App в аккаунте владельца приложения.")
        installation = installations[0]
        if installation["suspended_at"] is not None or installation["repository_selection"] not in ("all", "selected"):
            raise ProjectError("Включите установку GitHub App и разрешите доступ к репозиториям.")
        return installation["id"]

    def token(self, installation, repository=None):
        path = f"/app/installations/{installation}"
        jwt = self.jwt()
        # Listing needs only metadata. A sandbox receives only Contents access
        # for its repository, at the level currently granted to the installation.
        body = {"permissions": {"metadata": "read"}}
        if repository is not None:
            contents = request("GET", path, jwt)["permissions"].get("contents")
            if contents not in ("read", "write"):
                raise ProjectError("GitHub App требуется разрешение Contents.")
            body["permissions"]["contents"] = contents
            body["repository_ids"] = [repository]
        token = request("POST", path + "/access_tokens", jwt, body)["token"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,12000}", token):
            raise ProjectError(UNAVAILABLE)
        return token

    def repositories(self, installation):
        token = self.token(installation)
        page = 1
        while True:
            result = request("GET", f"/installation/repositories?per_page=100&page={page}", token)
            for repository in result["repositories"]:
                if repository["private"] and numeric_id(repository["id"]) and is_repository_name(repository["full_name"]):
                    yield {"id": repository["id"], "name": repository["full_name"]}
            if len(result["repositories"]) < 100:
                break
            page += 1


def exchange(path, request_data, timeout):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        connection.connect(path)
        connection.sendall(json.dumps(request_data).encode() + b"\n")
        with connection.makefile("rb") as reply:
            return json.loads(reply.readline(16384))


def provision(action, sandbox, repository=None):
    if exchange("/run/ai-bootstrap-projects.sock", {
        "action": action, "sandbox": sandbox, "repository": repository,
    }, 895) != {"ok": True}:
        raise ProjectError("Операция с песочницей не завершена.")


class Projects:
    def __init__(self, state, app, sandbox):
        self.state = state
        self.app = app
        self.sandbox = sandbox
        self.lock = Lock()
        self.jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="project")
        with closing(self.db()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS connection (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    app INTEGER NOT NULL,
                    installation INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    state TEXT NOT NULL
                );
                UPDATE project SET state = 'failed' WHERE state = 'creating';
                UPDATE project SET state = 'delete_failed' WHERE state = 'deleting';
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(project)")}
            if "canvas" not in columns:
                db.execute("ALTER TABLE project ADD COLUMN canvas TEXT NOT NULL DEFAULT 'pending'")
            db.execute("UPDATE project SET canvas = 'failed' WHERE canvas = 'connecting'")

    def db(self):
        return sqlite3.connect(self.state / "projects.sqlite3")

    def connection(self):
        with closing(self.db()) as db:
            return db.execute("SELECT app, installation FROM connection").fetchone()

    def installation(self, app):
        connection = self.connection()
        if not connection or connection[0] != app.id:
            raise ProjectError("Подключите настроенное GitHub App.")
        return connection[1]

    def connect(self):
        app = self.app()
        installation = app.installation()
        if not numeric_id(installation):
            raise ProjectError(UNAVAILABLE)
        with self.lock, closing(self.db()) as db, db:
            old = db.execute("SELECT app, installation FROM connection").fetchone()
            if old and old[0] != app.id:
                if db.execute("SELECT 1 FROM project LIMIT 1").fetchone():
                    raise ProjectError("Перед сменой установки GitHub App удалите подключённые проекты.")
            db.execute("INSERT OR REPLACE INTO connection VALUES (1, ?, ?)", (app.id, installation))
        return {}

    def list(self):
        with closing(self.db()) as db:
            rows = db.execute("SELECT id, name, state, canvas FROM project ORDER BY name COLLATE NOCASE").fetchall()
        return {
            "connected": self.connection() is not None,
            "projects": [{"id": key, "name": name, "state": state, "sandbox": f"gh-{key}", "canvas": canvas}
                         for key, name, state, canvas in rows],
        }

    def repositories(self):
        app = self.app()
        installation = self.installation(app)
        return {"repositories": sorted(app.repositories(installation), key=lambda repo: repo["name"].lower())}

    def add(self, repository):
        app = self.app()
        installation = self.installation(app)
        selected = next((repo for repo in app.repositories(installation) if repo["id"] == repository), None)
        if not selected:
            raise ProjectError(INVALID_PROJECT)
        with self.lock, closing(self.db()) as db, db:
            if self.connection() != (app.id, installation):
                raise ProjectError(INVALID_PROJECT)
            row = db.execute("SELECT state FROM project WHERE id = ?", (repository,)).fetchone()
            if row and row[0] in ("deleting", "delete_failed"):
                raise ProjectError("Завершите удаление проекта перед повторным добавлением.")
            if row and row[0] in ("creating", "ready"):
                return {}
            name = selected["name"]
            db.execute("INSERT OR REPLACE INTO project (id, name, state) VALUES (?, ?, 'creating')", (repository, name))
        self.jobs.submit(self.run_job, "create", repository, name)
        return {}

    def delete(self, repository):
        with self.lock, closing(self.db()) as db, db:
            row = db.execute("SELECT state, canvas FROM project WHERE id = ?", (repository,)).fetchone()
            if not row:
                raise ProjectError(INVALID_PROJECT)
            if row[0] == "deleting":
                return {}
            if row[0] == "creating" or row[1] == "connecting":
                raise ProjectError("Дождитесь окончания подготовки проекта.")
            db.execute("UPDATE project SET state = 'deleting' WHERE id = ?", (repository,))
        self.jobs.submit(self.run_job, "delete", repository, None)
        return {}

    def connect_canvas(self, repository):
        with self.lock, closing(self.db()) as db, db:
            row = db.execute("SELECT state, canvas FROM project WHERE id = ?", (repository,)).fetchone()
            if not row or row[0] != "ready":
                raise ProjectError(INVALID_PROJECT)
            if row[1] == "connecting":
                return {}
            db.execute("UPDATE project SET canvas = 'connecting' WHERE id = ?", (repository,))
        self.jobs.submit(self.run_canvas_job, repository)
        return {}

    def canvas_failed(self, repository):
        with self.lock, closing(self.db()) as db, db:
            db.execute("UPDATE project SET canvas = 'failed' WHERE id = ? AND canvas = 'ready'", (repository,))
        return {}

    def run_canvas_job(self, repository):
        try:
            self.sandbox("canvas", f"gh-{repository}", None)
        except (OSError, ValueError, ProjectError):
            state = "failed"
            print(f"Project gh-{repository}: Canvas connection failed", file=sys.stderr)
        else:
            state = "ready"
        with self.lock, closing(self.db()) as db, db:
            db.execute("UPDATE project SET canvas = ? WHERE id = ?", (state, repository))

    def run_job(self, action, repository, name):
        try:
            self.sandbox(action, f"gh-{repository}", name)
        except (OSError, ValueError, ProjectError):
            state = "failed" if action == "create" else "delete_failed"
            print(f"Project gh-{repository}: {action} failed", file=sys.stderr)
        else:
            state = "ready" if action == "create" else None
        with self.lock, closing(self.db()) as db, db:
            if state:
                db.execute("UPDATE project SET state = ? WHERE id = ?", (state, repository))
                if action == "create" and state == "ready":
                    db.execute("UPDATE project SET canvas = 'connecting' WHERE id = ?", (repository,))
            else:
                db.execute("DELETE FROM project WHERE id = ?", (repository,))
        if action == "create" and state == "ready":
            self.run_canvas_job(repository)


def credential(state, app_factory, username, path):
    sandbox = username.removeprefix("aisbx-")
    if not username.startswith("aisbx-") or not SANDBOX.fullmatch(sandbox) or not isinstance(path, str):
        return {}
    repository = int(sandbox[3:])
    if not numeric_id(repository):
        return {}
    with closing(sqlite3.connect(state / "projects.sqlite3")) as db:
        project = db.execute(
            """SELECT project.name, connection.app, connection.installation
               FROM project CROSS JOIN connection
               WHERE project.id = ? AND project.state IN ('creating', 'ready')""", (repository,),
        ).fetchone()
    if not project or path.lower() not in (project[0].lower(), project[0].lower() + ".git"):
        return {}
    app = app_factory()
    if app.id != project[1]:
        return {}
    return {"token": app.token(project[2], repository)}


def peer_uid(connection):
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def message(stream):
    line = stream.readline(2048)
    if not line.endswith(b"\n"):
        raise ValueError("Invalid request")
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("Invalid request")
    return data


def portal_request(projects, data):
    action = data.get("action")
    if action == "projects":
        return projects.list()
    if action == "app":
        return projects.app().setup_page()
    if action == "connect":
        return projects.connect()
    if action == "repositories":
        return projects.repositories()
    repository = data.get("repository")
    if numeric_id(repository):
        if action == "add":
            return projects.add(repository)
        if action == "delete":
            return projects.delete(repository)
        if action == "canvas":
            return projects.connect_canvas(repository)
        if action == "canvas-failed":
            return projects.canvas_failed(repository)
    raise ProjectError(INVALID_PROJECT)


def serve(projects):
    portal_uid = pwd.getpwnam("ai-portal").pw_uid
    slots = BoundedSemaphore(8)

    def reply(connection):
        try:
            with connection, connection.makefile("rb") as stream:
                try:
                    result = portal_request(projects, message(stream))
                except ProjectError as error:
                    result = {"error": str(error)}
                except (HTTPException, OSError, KeyError, TypeError, ValueError, sqlite3.Error):
                    result = {"error": UNAVAILABLE}
                connection.sendall(json.dumps(result).encode() + b"\n")
        except OSError:
            pass
        finally:
            slots.release()

    with socket.socket(fileno=3) as listener:
        while True:
            connection, _ = listener.accept()
            if peer_uid(connection) != portal_uid or not slots.acquire(blocking=False):
                connection.close()
                continue
            connection.settimeout(10)
            Thread(target=reply, args=(connection,), daemon=True).start()


def credential_request(app):
    with socket.fromfd(0, socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        username = pwd.getpwuid(peer_uid(connection)).pw_name
        return credential(STATE, app, username, message(sys.stdin.buffer).get("path"))


def main():
    def app():
        return GitHubApp(CONFIG)

    mode = sys.argv[1]
    if mode == "portal":
        projects = Projects(STATE, app, provision)
        for project in projects.list()["projects"]:
            if project["state"] == "ready" and project["canvas"] == "pending":
                projects.connect_canvas(project["id"])
        serve(projects)
        return
    try:
        result = credential_request(app)
    except (ProjectError, HTTPException, OSError, KeyError, TypeError, ValueError, sqlite3.Error):
        result = {}
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
