"""Password hashing and verification — the only module that touches bcrypt (#493).

bcrypt is deliberately slow (hundreds of ms per call) and CPU-bound. Called inline from an
`async def` route it freezes the event loop for that long: every other in-flight request
stalls, including ones waiting to return a pooled DB connection, and a login burst exhausts
the pool. So request-time hashing/verifying runs on a dedicated, bounded thread pool:

- dedicated, so a login burst can't starve the loop's default executor that other
  `asyncio.to_thread` users share;
- bounded by `BCRYPT_MAX_CONCURRENCY` (default: available CPUs), because threads beyond the
  core count add no bcrypt throughput. Work over the bound queues and waits — capacity is
  not unbounded. Callers must not hold a DB connection while awaiting (see routes/auth.py).
"""
import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import bcrypt

from app.config import settings

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _available_cpus() -> int:
    """CPUs this process may run on — respects affinity / a container cpuset, unlike cpu_count()."""
    process_cpu_count = getattr(os, "process_cpu_count", None)  # Python ≥ 3.13
    if process_cpu_count is not None:
        return process_cpu_count() or 1
    try:
        return len(os.sched_getaffinity(0)) or 1
    except AttributeError:  # not available on every platform (e.g. macOS)
        return os.cpu_count() or 1


def max_concurrency() -> int:
    return settings.BCRYPT_MAX_CONCURRENCY or _available_cpus()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(max_workers=max_concurrency(), thread_name_prefix="bcrypt")
    return _executor


def hash_password_blocking(password: str) -> str:
    """Hash on the calling thread. Only for code that runs before/outside request serving
    (startup admin seeding, the import-time dummy hash) — never call it from a route."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_blocking(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


async def hash_password(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(_get_executor(), hash_password_blocking, password)


async def verify_password(password: str, password_hash: str) -> bool:
    return await asyncio.get_running_loop().run_in_executor(
        _get_executor(), _verify_blocking, password, password_hash,
    )


def shutdown_executor() -> None:
    """Stop the worker threads (app shutdown). A later call re-creates the pool lazily."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
            _executor = None


def _reset_executor_for_tests() -> None:
    """Drop the pool so the next call picks up a changed BCRYPT_MAX_CONCURRENCY."""
    shutdown_executor()


# A fixed dummy hash (same cost factor as real hashes) compared on the login username-miss path
# so login spends the same bcrypt time whether or not the user exists — closes the timing
# oracle (#146). Computed once at import, not per request.
DUMMY_PASSWORD_HASH = hash_password_blocking("timing-equalizer")
