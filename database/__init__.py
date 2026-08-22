"""Database package.

Exposes a single shared :class:`Database` instance (``db``) and a
convenience ``migrate()`` that applies the schema.
"""

from __future__ import annotations

from .db import Database, settings
from .migrations import SCHEMA_VERSION, migrate

db = Database()

__all__ = ["Database", "db", "migrate", "SCHEMA_VERSION", "settings"]
