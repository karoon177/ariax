# -*- coding: utf-8 -*-
"""
Runtime singletons (avoids import cycles between main and API modules).

`app.main` populates DATABASE/PERSISTER at startup; every module reads
them through `get_db()` / `get_persister()`.
"""
from __future__ import annotations

from .db import Database, Persister

_DATABASE: Database | None = None
_PERSISTER: Persister | None = None


def set_db(db: Database) -> None:
    global _DATABASE
    _DATABASE = db


def set_persister(p: Persister) -> None:
    global _PERSISTER
    _PERSISTER = p


def get_db() -> Database:
    return _DATABASE


def get_persister() -> Persister:
    return _PERSISTER
