"""Database package.

Exposes the shared :class:`Database` singleton (``db``) used by the worker and
a convenience ``migrate()`` that applies the schema.
"""

from __future__ import annotations

from .db import Database, db, settings
from .migrations import SCHEMA_VERSION, migrate

__all__ = ["Database", "db", "migrate", "SCHEMA_VERSION"]

