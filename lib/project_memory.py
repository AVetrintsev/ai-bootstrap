"""Trusted project-scoped Graphiti service. Only MCP crosses the sandbox boundary."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import pwd
import re
import signal
import socket
import struct
import sys

from jsonschema import Draft202012Validator
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.nodes import EpisodeType, EpisodicNode
from falkordb.asyncio import FalkorDB
from redislite.falkordb_client import FalkorDB as EmbeddedFalkorDB


TOOLS = [
    {'name': 'add_memory', 'description': 'Save a durable memory for this project, shared by Codex and Qwen.',
     'inputSchema': {'type': 'object', 'additionalProperties': False,
        'properties': {'name': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                       'episode_body': {'type': 'string', 'minLength': 1, 'maxLength': 4000}},
        'required': ['name', 'episode_body']}},
    {'name': 'get_episodes', 'description': 'Read recent memories from this project.',
     'inputSchema': {'type': 'object', 'additionalProperties': False,
        'properties': {'num_results': {'type': 'integer', 'minimum': 1, 'maximum': 20}}}},
    {'name': 'search_memory_facts', 'description': 'Search facts in this project memory by meaning.',
     'inputSchema': {'type': 'object', 'additionalProperties': False,
        'properties': {'query': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
                       'num_results': {'type': 'integer', 'minimum': 1, 'maximum': 20}},
        'required': ['query']}},
    {'name': 'get_episode', 'description': 'Read a memory by its UUID within this project.',
     'inputSchema': {'type': 'object', 'additionalProperties': False,
        'properties': {'uuid': {'type': 'string', 'pattern': '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'}},
        'required': ['uuid']}},
    {'name': 'delete_episode', 'description': 'Delete a memory by its UUID within this project.',
     'inputSchema': {'type': 'object', 'additionalProperties': False,
        'properties': {'uuid': {'type': 'string', 'pattern': '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'}},
        'required': ['uuid']}},
]


class Memory:
    def __init__(self, project, directory, settings, key):
        if not re.fullmatch(r'gh-[1-9][0-9]{0,18}', project):
            raise ValueError('Invalid project')
        self.project = project
        self.directory = Path(directory)
        self.settings = settings
        self.key = key
        self.lock = asyncio.Lock()

    async def start(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.embedded = EmbeddedFalkorDB(str(self.directory / 'memory.rdb'),
                                        serverconfig={'port': '0', 'unixsocketperm': '700'})
        self.db = FalkorDB(unix_socket_path=self.embedded.client.socket_file)
        config = LLMConfig(api_key=self.key, base_url=self.settings['base_url'],
                           model=self.settings['model'], small_model=self.settings['model'])
        self.graph = Graphiti(
            graph_driver=FalkorDriver(falkor_db=self.db, database=self.project),
            llm_client=OpenAIGenericClient(config, max_tokens=4096),
            embedder=OpenAIEmbedder(OpenAIEmbedderConfig(api_key=self.key,
                base_url=self.settings['base_url'], embedding_model=self.settings['embedding_model'],
                embedding_dim=self.settings['embedding_dimensions'])),
            cross_encoder=OpenAIRerankerClient(config=config), max_coroutines=2)
        await self.graph.build_indices_and_constraints()

    async def close(self):
        await self.graph.close()
        await asyncio.to_thread(self.embedded.close)

    async def call(self, name, arguments):
        async with asyncio.timeout(240), self.lock:
            if name == 'add_memory':
                result = await self.graph.add_episode(name=arguments['name'],
                    episode_body=arguments['episode_body'], source_description='Project agent memory',
                    reference_time=datetime.now(timezone.utc), source=EpisodeType.text,
                    group_id=self.project)
                # A successful response means the entire graph snapshot is on disk.
                await self.db.connection.save()
                return {'uuid': result.episode.uuid, 'name': result.episode.name}
            if name == 'get_episode':
                item = await EpisodicNode.get_by_uuid(self.graph.driver, arguments['uuid'])
                return {'uuid': item.uuid, 'name': item.name, 'content': item.content}
            if name == 'delete_episode':
                await self.graph.remove_episode(arguments['uuid'])
                await self.db.connection.save()
                return {'deleted': arguments['uuid']}
            if name == 'search_memory_facts':
                facts = await self.graph.search(arguments['query'], group_ids=[self.project],
                                                num_results=int(arguments.get('num_results', 10)))
                return {'facts': [{'uuid': item.uuid, 'fact': item.fact,
                                   'episodes': item.episodes} for item in facts]}
            episodes = await self.graph.retrieve_episodes(datetime.now(timezone.utc),
                last_n=int(arguments.get('num_results', 10)), group_ids=[self.project])
            return {'episodes': [{'uuid': item.uuid, 'name': item.name, 'content': item.content}
                                 for item in episodes]}


async def serve(listener, memory, allowed_uid):
    validators = {tool['name']: Draft202012Validator(tool['inputSchema']) for tool in TOOLS}

    async def connect(reader, writer):
        peer = writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        if struct.unpack('3i', peer)[1] != allowed_uid:
            writer.close()
            await writer.wait_closed()
            return
        initialized = False
        try:
            while line := await reader.readline():
                identity = None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
                        raise ValueError
                    identity = request.get('id')
                    if identity is None and str(request.get('method', '')).startswith('notifications/'):
                        continue
                    if type(identity) not in (str, int):
                        raise ValueError
                    params = request.get('params', {})
                    if not isinstance(params, dict):
                        raise ValueError
                    method = request.get('method')
                    if method == 'initialize':
                        initialized = True
                        version = params.get('protocolVersion')
                        result = {'protocolVersion': version if version in ('2024-11-05', '2025-03-26', '2025-06-18') else '2025-03-26',
                                  'capabilities': {'tools': {}},
                                  'serverInfo': {'name': 'graphiti-project-memory', 'version': '1'}}
                    elif not initialized:
                        raise ValueError
                    elif method == 'ping':
                        result = {}
                    elif method == 'tools/list':
                        result = {'tools': TOOLS}
                    elif method == 'tools/call':
                        name, arguments = params.get('name'), params.get('arguments', {})
                        if (not isinstance(name, str) or name not in validators
                                or set(params) - {'name', 'arguments', '_meta'}
                                or not validators[name].is_valid(arguments)):
                            result = {'isError': True, 'content': [{'type': 'text',
                                'text': 'Unsupported tool or arguments. Memory is restricted to this project.'}]}
                        else:
                            try:
                                value = await memory.call(name, arguments)
                                result = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
                            except Exception:
                                # Provider errors can contain credentials, prompts and response bodies.
                                result = {'isError': True, 'content': [{'type': 'text',
                                    'text': 'Project memory operation failed. Retry after the service is restored.'}]}
                    else:
                        raise ValueError
                    response = {'jsonrpc': '2.0', 'id': identity, 'result': result}
                except (ValueError, TypeError):
                    response = {'jsonrpc': '2.0', 'id': identity,
                                'error': {'code': -32600, 'message': 'Invalid MCP request'}}
                writer.write(json.dumps(response).encode() + b'\n')
                await writer.drain()
        except (ConnectionError, ValueError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_unix_server(connect, sock=listener, limit=65536)


async def main():
    logging.disable(logging.CRITICAL)
    project = sys.argv[1]
    if not re.fullmatch(r'gh-[1-9][0-9]{0,18}', project):
        sys.exit('Invalid project')
    settings = json.loads(Path('/etc/ai-bootstrap/project-memory.json').read_text())
    key = (Path(os.environ['CREDENTIALS_DIRECTORY']) / 'provider.key').read_text().strip()
    memory = Memory(project, Path('/var/lib/ai-memory') / project, settings, key)
    await memory.start()
    listener = socket.socket(fileno=3)
    server = await serve(listener, memory, pwd.getpwnam('aisbx-' + project).pw_uid)
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    try:
        async with server:
            await stop.wait()
    finally:
        await memory.close()


if __name__ == '__main__':
    asyncio.run(main())
