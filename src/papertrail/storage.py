import asyncio
import logging
import shutil
from pathlib import Path

from papertrail.config import PapertrailConfig

logger = logging.getLogger(__name__)


class StorageSync:
    def __init__(self, config: PapertrailConfig):
        self.config = config
        self.local_root = config.data_dir
        self.remote_root = config.rclone_remote
        self._rclone_available: bool | None = None

    async def is_available(self) -> bool:
        """Check if rclone is installed and a remote is configured."""
        if self._rclone_available is not None:
            return self._rclone_available

        if not self.remote_root:
            self._rclone_available = False
            return False

        if not shutil.which("rclone"):
            logger.warning("rclone not found on PATH; sync disabled")
            self._rclone_available = False
            return False

        self._rclone_available = True
        return True

    async def pull(self) -> str:
        """Pull everything from remote to local."""
        if not await self.is_available():
            return "Sync not available (rclone not configured)"
        return await self._rclone("copy", self.remote_root, str(self.local_root))

    async def push(self) -> str:
        """Push everything from local to remote."""
        if not await self.is_available():
            return "Sync not available (rclone not configured)"
        return await self._rclone("copy", str(self.local_root), self.remote_root)

    async def push_file(self, local_path: Path, remote_subpath: str) -> str:
        """Push a single file to remote."""
        if not await self.is_available():
            return "Sync not available"
        remote = f"{self.remote_root}/{remote_subpath}"
        return await self._rclone("copyto", str(local_path), remote)

    async def pull_file(self, remote_subpath: str, local_path: Path) -> str:
        """Pull a single file from remote."""
        if not await self.is_available():
            return "Sync not available"
        remote = f"{self.remote_root}/{remote_subpath}"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        return await self._rclone("copyto", remote, str(local_path))

    async def sync_db(self, direction: str = "push") -> str:
        """Sync just the database file."""
        if not await self.is_available():
            return "Sync not available"
        local_db = str(self.config.db_path)
        remote_db = f"{self.remote_root}/db/papers.db"
        if direction == "push":
            return await self._rclone("copyto", local_db, remote_db)
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        return await self._rclone("copyto", remote_db, local_db)

    async def _rclone(self, command: str, source: str, dest: str) -> str:
        """Run an rclone command and return combined output."""
        proc = await asyncio.create_subprocess_exec(
            "rclone", command, source, dest,
            "--no-check-dest",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode() + stderr.decode()
        if proc.returncode != 0:
            error_msg = f"rclone {command} failed (exit {proc.returncode}): {output}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
        return output.strip()
