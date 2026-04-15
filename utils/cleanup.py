"""
Temp File & Memory Cleanup
===========================
Runs after every audit (success or failure) via the finally block,
and BEFORE every audit as a pre-flight safety check.

1. Kills ALL Chromium processes aggressively (SIGKILL, not SIGTERM)
2. Verifies no Chrome processes survive
3. Removes browser session artifacts from /tmp
4. Forces Python garbage collection + malloc_trim to return memory to OS
"""

import ctypes
import gc
import glob
import logging
import os
import shutil
import signal
import subprocess
import time

logger = logging.getLogger("agent.cleanup")


def full_cleanup():
    """Run all cleanup steps. Call from finally block after each audit."""
    kill_all_browsers()
    cleanup_browser_temp_files()
    reclaim_memory()


def preflight_cleanup():
    """Run before each audit to ensure a clean slate.
    
    More aggressive than full_cleanup: kills browsers, waits, verifies,
    and logs a warning if anything survived.
    """
    survivors = kill_all_browsers()
    if survivors > 0:
        # Give the OS a moment to reap, then try once more
        time.sleep(2)
        survivors = kill_all_browsers()
        if survivors > 0:
            logger.error(
                f"Pre-flight cleanup: {survivors} Chrome processes survived "
                f"two kill rounds. Proceeding anyway but OOM risk is elevated."
            )

    cleanup_browser_temp_files()
    reclaim_memory()

    # Brief pause to let the OS reclaim memory pages
    time.sleep(3)
    logger.info("Pre-flight cleanup complete")


def kill_all_browsers() -> int:
    """Kill ALL Chromium/Chrome processes inside the container.
    
    Uses SIGKILL directly (no graceful shutdown) because:
    - Chrome ignores SIGTERM when in certain states
    - Zombie renderer processes accumulate across audit cycles
    - The 208-OOM-kill incident on Apr 10-11 was caused by leaked Chrome processes
    
    Returns the number of surviving processes after the kill attempt.
    """
    killed = 0

    # Step 1: Find all Chrome/Chromium PIDs
    pids = _find_chrome_pids()
    if not pids:
        logger.info("No Chrome/Chromium processes found")
        return 0

    logger.info(f"Found {len(pids)} Chrome/Chromium process(es) to kill")

    # Step 2: SIGKILL all of them (no SIGTERM, no mercy)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            pass  # Already dead
        except PermissionError:
            logger.warning(f"Permission denied killing PID {pid}")
        except Exception as e:
            logger.warning(f"Failed to kill PID {pid}: {e}")

    if killed:
        logger.info(f"Sent SIGKILL to {killed} Chrome process(es)")

    # Step 3: Wait briefly for the kernel to reap
    time.sleep(1)

    # Step 4: Verify nothing survived
    survivors = _find_chrome_pids()
    if survivors:
        logger.warning(
            f"{len(survivors)} Chrome process(es) survived SIGKILL: "
            f"PIDs {survivors[:5]}{'...' if len(survivors) > 5 else ''}"
        )
        # Last resort: pkill -9 as a blanket
        try:
            subprocess.run(
                ["pkill", "-9", "-f", "chrom"],
                capture_output=True, timeout=5
            )
        except Exception:
            pass
        time.sleep(1)
        survivors = _find_chrome_pids()

    return len(survivors) if survivors else 0


def _find_chrome_pids() -> list[int]:
    """Find all PIDs matching Chrome/Chromium process patterns."""
    pids = set()
    patterns = ["chromium", "chrome", "headless_shell"]

    for pattern in patterns:
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line.strip():
                        try:
                            pids.add(int(line.strip()))
                        except ValueError:
                            pass
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        except Exception as e:
            logger.warning(f"Error finding Chrome PIDs with pattern '{pattern}': {e}")

    # Don't kill our own Python process
    my_pid = os.getpid()
    pids.discard(my_pid)

    return sorted(pids)


def cleanup_browser_temp_files():
    """Remove all browser session artifacts from /tmp."""
    patterns = [
        "/tmp/browser-use-user-data-dir-*",
        "/tmp/browser_use_agent_*",
        "/tmp/playwright-*",
        "/tmp/.com.google.Chrome.*",
        "/tmp/chromium-*",
        "/tmp/Crashpad*",
        "/tmp/.org.chromium.*",
    ]

    removed_dirs = 0
    removed_files = 0
    freed_bytes = 0

    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                if os.path.isdir(path):
                    size = _dir_size(path)
                    shutil.rmtree(path, ignore_errors=True)
                    removed_dirs += 1
                    freed_bytes += size
                else:
                    freed_bytes += os.path.getsize(path)
                    os.remove(path)
                    removed_files += 1
            except Exception as e:
                logger.warning(f"Failed to remove {path}: {e}")

    for ext in ("*.png", "*.jpg", "*.webp"):
        for path in glob.glob(f"/tmp/{ext}"):
            try:
                freed_bytes += os.path.getsize(path)
                os.remove(path)
                removed_files += 1
            except Exception:
                pass

    freed_mb = freed_bytes / (1024 * 1024)
    if removed_dirs or removed_files:
        logger.info(
            f"Cleanup: removed {removed_dirs} dirs + {removed_files} files, "
            f"freed {freed_mb:.1f} MB"
        )
    else:
        logger.info("Cleanup: no temp files to remove")


def reclaim_memory():
    """Force Python to release memory back to the OS.

    Python's memory allocator holds onto freed blocks for reuse.
    After processing large objects (160KB surge data, browser state),
    this can leave hundreds of MB 'used' in RSS that is actually free
    internally. gc.collect() frees the Python objects, and malloc_trim()
    tells glibc to return unused heap pages to the OS.
    """
    collected = gc.collect(2)

    trimmed = False
    try:
        libc = ctypes.CDLL("libc.so.6")
        result = libc.malloc_trim(0)
        trimmed = result == 1
    except Exception:
        pass

    logger.info(
        f"Memory reclaim: gc collected {collected} objects, "
        f"malloc_trim={'released memory' if trimmed else 'no change'}"
    )


def _dir_size(path: str) -> int:
    """Calculate total size of a directory tree."""
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total
