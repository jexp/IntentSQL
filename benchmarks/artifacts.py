"""Shared location for generated benchmark data outside the source tree."""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def artifact_root() -> Path:
    """Return the external artifact directory, with a portable override."""
    configured = os.environ.get("INTENTSQL_ARTIFACTS")
    return (Path(configured).expanduser() if configured else
            ROOT.parent / f"{ROOT.name}-artifacts")
