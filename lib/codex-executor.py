"""Trusted socket to an executor process in exactly one rootless project."""
import asyncio
from contextlib import suppress
import json
import os
import pwd
import re
import socket


WORKSPACE_CHECK = '''
import json, pathlib, subprocess, sys
path = pathlib.Path(sys.argv[1])
resolved = path.resolve(strict=True)
source = pathlib.Path('/workspace/repo').resolve(strict=True)
roots = [source]
result = subprocess.run(['git', '-C', str(source), 'worktree', 'list', '--porcelain', '-z'],
                        capture_output=True, check=True)
for field in result.stdout.decode().split('\\0'):
    if field.startswith('worktree '):
        root = pathlib.Path(field[9:]).resolve(strict=True)
        if root.is_relative_to('/workspace/worktrees'):
            roots.append(root)
if str(resolved) != str(path) or not resolved.is_dir() or not any(resolved.is_relative_to(root) for root in roots):
    sys.exit(1)
print(json.dumps({'cwd': str(resolved)}))
'''


async def main():
    account = pwd.getpwuid(os.getuid())
    if not re.fullmatch(r'aisbx-gh-[1-9][0-9]{0,18}', account.pw_name):
        raise ValueError('Expected project account')
    environment = {'PATH': '/usr/bin:/bin', 'HOME': account.pw_dir,
                   'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}'}
    slots = asyncio.Semaphore(4)

    async def connect(reader, writer):
        async with slots:
            child = None
            tasks = []
            try:
                first = await asyncio.wait_for(reader.readline(), 15)
                if first.startswith(b'AI-WORKSPACE '):
                    cwd = json.loads(first[len(b'AI-WORKSPACE '):])
                    if not isinstance(cwd, str):
                        return
                    child = await asyncio.create_subprocess_exec(
                        'podman', 'exec', 'project', 'python3', '-I', '-c', WORKSPACE_CHECK, cwd,
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, env=environment)
                    output, _ = await asyncio.wait_for(child.communicate(), 10)
                    writer.write(output if child.returncode == 0 else b'{"error":"invalid workspace"}\n')
                    await writer.drain()
                    return
                if not first:
                    return
                child = await asyncio.create_subprocess_exec(
                    'podman', 'exec', '--interactive', '--workdir', '/workspace/repo',
                    '--env', 'CODEX_HOME=/tmp/ai-codex-executor', '--env', 'TOKIO_WORKER_THREADS=2',
                    'project', '/opt/ai-codex/bin/codex', 'exec-server', '--listen', 'stdio',
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=environment)
                child.stdin.write(first)
                await child.stdin.drain()

                async def copy(source, target):
                    while data := await source.read(65536):
                        target.write(data)
                        await target.drain()

                tasks = [asyncio.create_task(copy(reader, child.stdin)), asyncio.create_task(copy(child.stdout, writer))]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            except (OSError, ValueError, asyncio.TimeoutError):
                pass
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                writer.close()
                if child is not None and child.returncode is None:
                    if child.stdin:
                        child.stdin.close()
                    async def drain_and_wait():
                        while await child.stdout.read(65536):
                            pass
                        await child.wait()
                    try:
                        await asyncio.wait_for(drain_and_wait(), 5)
                    except asyncio.TimeoutError:
                        with suppress(ProcessLookupError):
                            child.kill()
                        with suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(drain_and_wait(), 5)

    server = await asyncio.start_unix_server(connect, sock=socket.socket(fileno=3), limit=16 * 1024 * 1024)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
