"""Sync local data directory with an rclone remote."""

import asyncio
import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SYNC_MAX_AGE_SECONDS = 300  # re-pull if last sync was more than 5 minutes ago


def _has_rclone() -> bool:
    if shutil.which("rclone"):
        return True
    logger.warning("rclone not found on PATH; running with local files only")
    return False


async def _run_rclone(*args: str) -> bool:
    """Run an rclone command and return True on success."""
    cmd = ["rclone", *args]
    logger.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("rclone failed (exit %d): %s", proc.returncode, stderr.decode().strip())
        return False
    return True


async def sync_pull(remote: str, data_dir: Path) -> None:
    """Pull remote state to local data directory. No-op if remote is empty."""
    if not remote or not _has_rclone():
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    await _run_rclone("sync", remote.rstrip("/"), str(data_dir))


async def sync_pull_if_stale(remote: str, data_dir: Path, last_pull_time: float) -> float:
    """Re-pull if enough time has passed since the last pull.

    Returns the updated last_pull_time (current time if a pull happened,
    or the original value if skipped).
    """
    if not remote:
        return last_pull_time
    elapsed = time.monotonic() - last_pull_time
    if elapsed < SYNC_MAX_AGE_SECONDS:
        return last_pull_time
    logger.info("Last sync was %.0fs ago, re-pulling", elapsed)
    await sync_pull(remote, data_dir)
    return time.monotonic()


async def sync_push(remote: str, data_dir: Path, subpath: str) -> None:
    """Push a local subpath back to the remote. No-op if remote is empty."""
    if not remote or not _has_rclone():
        return
    local = data_dir / subpath
    remote_path = f"{remote.rstrip('/')}/{subpath}"
    if local.is_file():
        await _run_rclone("copyto", str(local), remote_path)
    elif local.is_dir():
        await _run_rclone("copy", str(local), remote_path)
    else:
        logger.warning("sync_push: local path does not exist: %s", local)


async def sync_delete(remote: str, subpath: str) -> None:
    """Delete a subpath from the remote. No-op if remote is empty."""
    if not remote or not _has_rclone():
        return
    remote_path = f"{remote.rstrip('/')}/{subpath}"
    await _run_rclone("purge", remote_path)
