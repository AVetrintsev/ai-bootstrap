"""Immutable proposals and owner-approved Graphiti episodes, outside all sandboxes."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import pwd
import signal
import socket
import sqlite3
import struct
import sys
from uuid import uuid4

from falkordb.asyncio import FalkorDB
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.nodes import EpisodeType, EpisodicNode
from jsonschema import Draft202012Validator
from redislite.falkordb_client import FalkorDB as EmbeddedFalkorDB

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared_memory_client import SHARED_TOOLS, UUID_SCHEMA


class SharedMemory:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = asyncio.Lock()

    async def start(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self.connect()) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS proposals (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
                project TEXT NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL,
                revision TEXT NOT NULL, created_at TEXT NOT NULL, published_at TEXT)''')
            db.commit()
        self.embedded = EmbeddedFalkorDB(str(self.directory / 'memory.rdb'),
                                        serverconfig={'port': '0', 'unixsocketperm': '700'})
        self.db = FalkorDB(unix_socket_path=self.embedded.client.socket_file)
        self.driver = FalkorDriver(falkor_db=self.db, database='shared')

    def connect(self):
        db = sqlite3.connect(self.directory / 'proposals.sqlite3')
        db.row_factory = sqlite3.Row
        return db

    async def close(self):
        await self.driver.close()
        await asyncio.to_thread(self.embedded.close)

    async def call(self, action, fields):
        async with asyncio.timeout(15), self.lock:
            if action == 'propose_shared_memory':
                identity = str(uuid4())
                revision = hashlib.sha256(json.dumps(
                    [fields['name'], fields['episode_body']], ensure_ascii=False).encode()).hexdigest()
                with closing(self.connect()) as db, db:
                    db.execute('''INSERT INTO proposals
                        (uuid, project, name, content, revision, created_at) VALUES (?, ?, ?, ?, ?, ?)''',
                        (identity, fields['project'], fields['name'], fields['episode_body'],
                         revision, datetime.now(timezone.utc).isoformat()))
                return {'uuid': identity, 'status': 'pending', 'name': fields['name']}
            if action == 'list_proposals':
                with closing(self.connect()) as db:
                    rows = db.execute('''SELECT * FROM proposals WHERE published_at IS NULL
                        AND sequence > ? ORDER BY sequence LIMIT 21''', (fields.get('after', 0),)).fetchall()
                return {'proposals': [dict(row) for row in rows[:20]],
                        'next_after': rows[19]['sequence'] if len(rows) > 20 else None}
            if action == 'approve_proposal':
                with closing(self.connect()) as db:
                    row = db.execute('SELECT * FROM proposals WHERE uuid = ?', (fields['uuid'],)).fetchone()
                if row is None or not hmac.compare_digest(row['revision'], fields['revision']):
                    raise ValueError('Предложение не найдено или текст не совпадает. Откройте список заново.')
                if row['published_at'] is None:
                    # No model rewrites or expands the text the owner approved.
                    # A stable UUID makes retry after a crash idempotent, including
                    # a crash between the graph snapshot and the SQLite commit.
                    episode = EpisodicNode(uuid=row['uuid'], name=row['name'], group_id='shared',
                        source=EpisodeType.text, source_description='Owner approved; project ' + row['project'],
                        content=row['content'], valid_at=datetime.fromisoformat(row['created_at']))
                    await episode.save(self.driver)
                    await self.db.connection.save()
                    with closing(self.connect()) as db, db:
                        db.execute('UPDATE proposals SET published_at = ? WHERE uuid = ?',
                                   (datetime.now(timezone.utc).isoformat(), row['uuid']))
                return {'uuid': row['uuid'], 'status': 'published'}
            if action == 'get_shared_memory':
                item = await EpisodicNode.get_by_uuid(self.driver, fields['uuid'])
                return {'uuid': item.uuid, 'name': item.name, 'content': item.content}
            if action == 'get_shared_memories':
                limit = fields.get('num_results', 10)
                episodes = await EpisodicNode.get_by_group_ids(self.driver, ['shared'],
                    limit=limit + 1, uuid_cursor=fields.get('cursor'))
                return {'episodes': [{'uuid': item.uuid, 'name': item.name, 'content': item.content}
                                     for item in episodes[:limit]],
                        'next_cursor': episodes[limit - 1].uuid if len(episodes) > limit else None}
            raise ValueError('Unknown shared memory action')


async def serve(listener, memory, allowed_uid, *, owner=False):
    schemas = {
        tool['name']: {**tool['inputSchema'],
            'properties': {**tool['inputSchema']['properties'],
                           'project': {'type': 'string', 'pattern': '^gh-[1-9][0-9]{0,18}$'}},
            'required': [*tool['inputSchema'].get('required', []), 'project']}
        for tool in SHARED_TOOLS
    } if not owner else {
        'list_proposals': {'type': 'object', 'additionalProperties': False,
            'properties': {'after': {'type': 'integer', 'minimum': 0, 'maximum': 9223372036854775807}}},
        'approve_proposal': {'type': 'object', 'additionalProperties': False,
            'properties': {'uuid': UUID_SCHEMA,
                'revision': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
            'required': ['uuid', 'revision']},
    }
    validators = {name: Draft202012Validator(schema) for name, schema in schemas.items()}

    async def connect(reader, writer):
        result = None
        try:
            peer = writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack('3i', peer)[1] != allowed_uid:
                return
            async with asyncio.timeout(20):
                message = json.loads(await reader.readline())
                if not isinstance(message, dict):
                    raise ValueError
                action = message.pop('action', None)
                if not isinstance(action, str) or action not in validators or not validators[action].is_valid(message):
                    raise ValueError
                result = await memory.call(action, message)
        except Exception:
            result = {'error': 'Операция общей памяти недоступна. Обновите страницу и повторите попытку.'}
        finally:
            if result is not None:
                try:
                    writer.write(json.dumps(result).encode() + b'\n')
                    await writer.drain()
                except ConnectionError:
                    pass
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_unix_server(connect, sock=listener, limit=32768)


async def main():
    logging.disable(logging.CRITICAL)
    memory = SharedMemory('/var/lib/ai-memory/shared')
    await memory.start()
    servers = []
    try:
        names = os.environ.get('LISTEN_FDNAMES', '').split(':')
        if sorted(names) != ['agents', 'owner']:
            raise ValueError('Expected the agent and owner systemd sockets')
        for fd, name in enumerate(names, 3):
            owner = name == 'owner'
            servers.append(await serve(socket.socket(fileno=fd), memory,
                pwd.getpwnam('ai-portal' if owner else 'ai-memory').pw_uid, owner=owner))
        stop = asyncio.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
        await stop.wait()
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()
        await memory.close()


if __name__ == '__main__':
    asyncio.run(main())
