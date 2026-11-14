"""Small, credential-free client for the trusted shared-memory service."""
import json
import socket


AGENT_SOCKET = '/run/ai-bootstrap-shared-memory.sock'
OWNER_SOCKET = '/run/ai-bootstrap-shared-memory-owner.sock'
UUID_SCHEMA = {'type': 'string', 'pattern': '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'}
# HTML normalizes carriage returns and cannot display most control characters.
TEXT_SCHEMA = {'type': 'string', 'minLength': 1, 'maxLength': 4000,
               'pattern': r'^[^\x00-\x08\x0b-\x1f\x7f]+$'}
SHARED_TOOLS = [
    {'name': 'propose_shared_memory',
     'description': 'Propose exact text for shared memory. Only the owner can publish it in the portal.',
     'inputSchema': {'type': 'object', 'additionalProperties': False,
         'properties': {'name': {**TEXT_SCHEMA, 'maxLength': 160}, 'episode_body': TEXT_SCHEMA},
         'required': ['name', 'episode_body']}},
    {'name': 'get_shared_memories',
     'description': 'Read owner-approved memories shared across all projects. Follow next_cursor for more.',
     'inputSchema': {'type': 'object', 'additionalProperties': False,
         'properties': {'num_results': {'type': 'integer', 'minimum': 1, 'maximum': 20},
                        'cursor': UUID_SCHEMA}}},
    {'name': 'get_shared_memory',
     'description': 'Read the exact owner-approved shared memory by UUID. Pending proposals are unavailable.',
     'inputSchema': {'type': 'object', 'additionalProperties': False,
         'properties': {'uuid': UUID_SCHEMA}, 'required': ['uuid']}},
]


def request(action, *, socket_path=AGENT_SOCKET, **fields):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(20)
        connection.connect(socket_path)
        connection.sendall(json.dumps({'action': action, **fields}, ensure_ascii=False).encode() + b'\n')
        with connection.makefile('rb') as reply:
            result = json.loads(reply.readline(1024 * 1024))
    if 'error' in result:
        raise ValueError(result['error'])
    return result
