"""The state kept between runs: what patchwork and notmuch last agreed on."""

import os

import peewee

from . import Error

SCHEMA_VERSION = 1

database = peewee.SqliteDatabase(None)


class StoreError(Error):
    """The store was written by another schema version."""


class Patch(peewee.Model):
    """A synced patch and the state both sides agreed on at the end of the last run.

    Telling which side moved since then decides the direction of the sync.
    """

    id = peewee.IntegerField(primary_key=True)  # patchwork's patch id
    project = peewee.TextField(index=True)
    state = peewee.TextField()

    class Meta:
        database = database


def open_store(path: str, *, dry_run: bool = False) -> None:
    """Open the store at `path`, creating it if needed; a dry run creates nothing on disk."""
    if dry_run and not os.path.exists(path):
        path = ":memory:"
    database.init(path)
    version = database.pragma("user_version")
    if version == SCHEMA_VERSION:
        return
    if version or database.get_tables():
        raise StoreError(
            f"{path}: holds schema version {version}, not {SCHEMA_VERSION}; delete it to start over"
        )
    database.create_tables([Patch])
    database.pragma("user_version", SCHEMA_VERSION)
