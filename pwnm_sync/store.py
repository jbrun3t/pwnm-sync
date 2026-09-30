"""The state kept between runs: what patchwork and notmuch last agreed on."""

import contextlib
import os

import peewee

from . import Error

SCHEMA_VERSION = 6

database = peewee.SqliteDatabase(None, pragmas={"foreign_keys": 1})


class StoreError(Error):
    """The store could not be opened, or was written by another schema version."""


class BaseModel(peewee.Model):
    class Meta:
        database = database


class User(BaseModel):
    """A patchwork user: tags name a delegate by username, patchwork takes its user id."""

    id = peewee.IntegerField(primary_key=True)  # patchwork's user id
    username = peewee.TextField(unique=True)


class Patch(BaseModel):
    """A synced patch and the values both sides agreed on at the end of the last run.

    Telling which side moved since then decides the direction of the sync. A patch whose
    message is not in notmuch yet is kept untagged, with patchwork's values.
    """

    id = peewee.IntegerField(primary_key=True)  # patchwork's patch id
    project = peewee.TextField(index=True)
    msgid = peewee.TextField()
    state = peewee.TextField()
    delegate = peewee.ForeignKeyField(User, null=True)
    tagged = peewee.BooleanField()


class Project(BaseModel):
    """The newest patch and event of a project already read: later runs read what follows."""

    name = peewee.TextField(primary_key=True)
    patch = peewee.IntegerField()
    event = peewee.IntegerField()


def open_store(path: str, *, dry_run: bool = False) -> None:
    """Open the store at `path`, creating it if needed; a dry run creates nothing on disk."""
    path = os.path.expanduser(path)
    try:
        if dry_run and not os.path.exists(path):
            path = ":memory:"
        else:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        database.init(path)
        version = database.pragma("user_version")
        if version == SCHEMA_VERSION:
            return
        if version or database.get_tables():
            raise StoreError(
                f"{path}: holds schema version {version}, not {SCHEMA_VERSION}; "
                "delete it to start over"
            )
        database.create_tables([User, Patch, Project])
        database.pragma("user_version", SCHEMA_VERSION)
    except (OSError, peewee.PeeweeException) as e:
        raise StoreError(f"{path}: {e}") from e


@contextlib.contextmanager
def transaction(dry_run: bool = False):
    """Group the run's writes to the store, undoing them at the end of a dry run."""
    with database.atomic() as writes:
        yield
        if dry_run:
            writes.rollback()
