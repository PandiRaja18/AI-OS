"""Object storage for large payloads.

Checkpoints are written on every superstep, so anything large in run state is
written dozens of times per run. Reports and long evidence live here instead and
the state keeps a URI. This is the single biggest write-amplification trap in the
design, and this module is how it is avoided.
"""

from __future__ import annotations

import re
from pathlib import Path

SCHEME = "aios://"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class ObjectNotFound(KeyError):
    """No object at that URI."""


class FileObjectStore:
    """Local-filesystem object store. Swap for S3 by reimplementing put and get."""

    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def put(self, tenant_id: str, run_id: str, name: str, body: str) -> str:
        """Store a payload and return its URI."""
        key = f"{_safe(tenant_id)}/{_safe(run_id)}/{_safe(name)}"
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return f"{SCHEME}{key}"

    def get(self, uri: str) -> str:
        path = self._path_for(uri)
        if not path.exists():
            raise ObjectNotFound(uri)
        return path.read_text(encoding="utf-8")

    def exists(self, uri: str) -> bool:
        return self._path_for(uri).exists()

    def _path_for(self, uri: str) -> Path:
        if not uri.startswith(SCHEME):
            raise ObjectNotFound(f"not an object URI: {uri}")
        return self._root / uri.removeprefix(SCHEME)


def _safe(value: str) -> str:
    return _SAFE.sub("_", value)
