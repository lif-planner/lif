import os
import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def app_version():
    return os.environ.get("LIF_VERSION", "1.1.10")


@lru_cache(maxsize=1)
def git_commit():
    configured = os.environ.get("LIF_GIT_COMMIT")
    if configured:
        return configured
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def version_context():
    return {
        "app_version": app_version(),
        "git_commit": git_commit(),
    }
