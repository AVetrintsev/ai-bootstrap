"""Pinned CGC entry point: observe edits before synchronizing an existing graph."""
import asyncio
from pathlib import Path

from codegraphcontext.core.watcher import CodeWatcher
from codegraphcontext.core.database_falkordb import FalkorDBManager
import codegraphcontext.server as cgc_server


class ProjectWatcher(CodeWatcher):
    def watch_directory(self, path, perform_initial_scan=True, cgcignore_path=None, sync_on_start=False):
        path = str(Path(path).resolve())
        if path in self.watched_paths or not sync_on_start:
            return super().watch_directory(path, perform_initial_scan, cgcignore_path, sync_on_start)
        # CGC 0.6.13 normally scans in the handler constructor, before scheduling
        # its observer. Schedule first; queued file events run after the full scan.
        result = super().watch_directory(path, False, cgcignore_path, False)
        handler = self.handlers[path]
        with handler._update_lock:
            handler.synchronize_with_disk()
        return result


async def main():
    # Pin the factory before construction: CGC's automatic fallback can open
    # graph.rdb with another database format, preventing a later retry.
    cgc_server.get_database_manager = FalkorDBManager
    server = cgc_server.MCPServer(loop=asyncio.get_running_loop(), cwd=Path('/workspace/repo'))
    server.code_watcher = ProjectWatcher(server.graph_builder, server.job_manager)
    await server.run()


if __name__ == '__main__':
    asyncio.run(main())
