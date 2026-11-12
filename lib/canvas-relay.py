"""Socket-activated raw transport into a single user's rootless container."""
import asyncio
import os
import socket


async def main():
    slots = asyncio.Semaphore(32)
    environment = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}")

    async def relay(reader, writer):
        async with slots:
            process = await asyncio.create_subprocess_exec(
                "podman", "exec", "--interactive", "project", "socat", "STDIO", "TCP:127.0.0.1:18000",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=environment)

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            tasks = [asyncio.create_task(copy(reader, process.stdin)),
                     asyncio.create_task(copy(process.stdout, writer))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                writer.close()
                if process.returncode is None:
                    process.terminate()
                await process.wait()

    listener = socket.socket(fileno=3)
    server = await asyncio.start_unix_server(relay, sock=listener)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
