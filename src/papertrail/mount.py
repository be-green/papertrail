"""Auto-mount/unmount rclone remote at the data directory."""

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

MOUNT_READY_TIMEOUT = 15  # seconds to wait for mount to become ready


def _is_mountpoint(path: Path) -> bool:
    """Check if a path is a mountpoint (works on macOS and Linux)."""
    if not path.exists():
        return False
    return os.path.ismount(str(path))


async def mount_rclone(remote: str, mount_path: Path) -> asyncio.subprocess.Process | None:
    """Mount an rclone remote at mount_path. Returns the process or None if skipped.

    Skips mounting if:
    - remote is empty
    - rclone is not installed
    - mount_path is already a mountpoint
    """
    if not remote:
        return None

    if not shutil.which("rclone"):
        logger.warning("rclone not found on PATH; running with local files only")
        return None

    if _is_mountpoint(mount_path):
        logger.info("Already mounted at %s, skipping mount", mount_path)
        return None

    mount_path.mkdir(parents=True, exist_ok=True)

    vfs_cache_mode = "writes"
    cmd = [
        "rclone", "mount",
        remote.rstrip("/"),
        str(mount_path),
        "--vfs-cache-mode", vfs_cache_mode,
        "--vfs-write-back", "5s",
        "--dir-cache-time", "30s",
        "--allow-non-empty",
    ]

    if sys.platform == "darwin":
        cmd.append("--volname=papertrail")

    logger.info("Mounting %s at %s", remote, mount_path)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Wait for the mount to become available
    for attempt in range(MOUNT_READY_TIMEOUT * 10):
        if proc.returncode is not None:
            stderr = (await proc.stderr.read()).decode()
            logger.error("rclone mount exited immediately: %s", stderr)
            return None
        if _is_mountpoint(mount_path):
            logger.info("Mount ready at %s (took %.1fs)", mount_path, attempt / 10)
            return proc
        await asyncio.sleep(0.1)

    logger.error("Mount at %s did not become ready within %ds", mount_path, MOUNT_READY_TIMEOUT)
    proc.terminate()
    return None


async def unmount_rclone(mount_path: Path, proc: asyncio.subprocess.Process | None) -> None:
    """Unmount an rclone mount. Tries fusermount/umount, then terminates the process."""
    if proc is None:
        return

    if not _is_mountpoint(mount_path):
        logger.info("Not a mountpoint, skipping unmount: %s", mount_path)
        if proc.returncode is None:
            proc.terminate()
        return

    logger.info("Unmounting %s", mount_path)

    # Use platform-appropriate unmount
    if sys.platform == "darwin":
        unmount_cmd = ["umount", str(mount_path)]
    else:
        fusermount = shutil.which("fusermount")
        if fusermount:
            unmount_cmd = ["fusermount", "-u", str(mount_path)]
        else:
            unmount_cmd = ["umount", str(mount_path)]

    try:
        unmount_proc = await asyncio.create_subprocess_exec(
            *unmount_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(unmount_proc.communicate(), timeout=10)
    except (asyncio.TimeoutError, OSError) as exc:
        logger.warning("Unmount command failed: %s", exc)

    # Ensure the rclone process exits
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
