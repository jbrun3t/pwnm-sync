"""Syncing a patchwork project with notmuch, from what both sides last agreed on."""

import datetime
import itertools

import click
import notmuch2
import peewee

from .config import WINDOW
from .patchwork import PwError
from .store import Patch, Project, User
from .tags import Tags

# The events synced, and the field they change
EVENTS = {"patch-state-changed": "state", "patch-delegated": "delegate"}


def open_notmuch(config):
    modes = notmuch2.Database.MODE
    mode = modes.READ_ONLY if config.dry_run else modes.READ_WRITE
    return notmuch2.Database(config.notmuch, mode=mode)


def username(user):
    """The username of a user patchwork embeds in a document, recording the user."""
    if not user:
        return None
    User.get_or_create(id=user["id"], defaults={"username": user["username"]})
    return user["username"]


def patch_values(patch):
    """The patch's row, from its patchwork document."""
    return {
        "id": patch["id"],
        "msgid": patch["msgid"][1:-1],
        "state": patch["state"],
        "delegate": username(patch["delegate"]),
    }


def row_values(row):
    return {
        "id": row.id,
        "msgid": row.msgid,
        "state": row.state,
        "delegate": row.delegate.username if row.delegate else None,
    }


def newest_id(entries):
    return next((e["id"] for e in entries), 0)


def after(entries, cursor):
    """The entries listed newest first down to id `cursor`, oldest first, and the newest id.

    An entry listed twice, as new ones push the pages down, is taken once.
    """
    found = {e["id"]: e for e in itertools.takewhile(lambda e: e["id"] > cursor, entries)}
    return list(reversed(found.values())), max(found, default=cursor)


def replay(remote, events):
    """Apply the synced events, oldest first, to the patches of `remote`.

    Returns the msgids of the patches `remote` lacks, by id.
    """
    unknown = {}
    for event in events:
        field = EVENTS.get(event["category"])
        if field is None:
            continue
        patch = event["payload"]["patch"]
        value = event["payload"]["current_" + field]
        if patch["id"] in remote:
            remote[patch["id"]][field] = username(value) if field == "delegate" else value
        else:
            unknown[patch["id"]] = patch["msgid"][1:-1]
    return unknown


def merge(remote, stored, tagged, source=None):
    """The tagged values to push to patchwork, and the fields whose tags are ambiguous.

    Each field is reconciled on its own from its base, the value both sides last agreed on.
    The tags win when they moved from it to a single new value and patchwork did not;
    patchwork keeps its value otherwise. `source` notmuch takes patchwork's values as the
    base, so any other tagged value wins; `source` patchwork, or nothing stored, pushes
    nothing.
    """
    push, ambiguous = {}, {}
    if source == "patchwork" or (stored is None and source != "notmuch"):
        return push, ambiguous
    base = remote if source == "notmuch" else stored
    for field, values in tagged.items():
        if remote[field] != base[field]:
            continue  # patchwork moved
        moved = values - {base[field]}
        if len(moved) == 1:
            push[field] = moved.pop()
        elif moved:
            ambiguous[field] = moved
    return push, ambiguous


class ProjectSync:
    """Syncs one patchwork project's patches with their notmuch tags."""

    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.project = client.project_name
        self.tags = Tags(config, client.project_name)

    def run(self, epoch=None):
        """Sync the stored patches and the ones read since the last run.

        The first run reads the patches since `epoch`, or the last year; a later one reads
        the events and the new patches, and the patches since `epoch` when given.
        """
        click.echo(f"Looking at project {self.project}")
        cursors = Project.get_or_none(Project.name == self.project)
        first = cursors is None
        if first:
            self.client.project()  # a misspelt project would list nothing, silently
            # Read before the patches, so the next run replays what moves meanwhile
            cursors = Project(
                name=self.project,
                patch=newest_id(self.client.patches(order="-id", per_page=1)),
                event=newest_id(self.client.events(per_page=1)),
            )
            since = epoch or datetime.datetime.now(datetime.UTC) - WINDOW
            listed = list(self.client.patches(since=since))
            events = []
        else:
            events, cursors.event = after(
                self.client.events(per_page=self.config.batch), cursors.event
            )
            listed, cursors.patch = after(self.client.patches(order="-id"), cursors.patch)
            if epoch:
                listed += self.client.patches(since=epoch)

        query = (
            Patch.select(Patch, User)
            .join(User, peewee.JOIN.LEFT_OUTER)
            .where(Patch.project == self.project)
        )
        rows = {row.id: row for row in query}
        remote = {patch_id: row_values(row) for patch_id, row in rows.items()}
        for patch in listed:
            remote[patch["id"]] = patch_values(patch)
        unknown = replay(remote, events)

        for batch in itertools.batched(remote.values(), self.config.batch):
            # We open the DB for each batch as to not hold the notmuch
            # database open blocking other writers for too long.
            with open_notmuch(self.config) as db, db.atomic():
                for values in batch:
                    self.sync_patch(db, values, rows.get(values["id"]))
        if unknown:
            with open_notmuch(self.config) as db, db.atomic():
                for patch_id, msgid in unknown.items():
                    self.adopt(db, patch_id, msgid)

        cursors.save(force_insert=first)
        click.echo(f"Finished processing {len(remote)} {self.project} patches!")

    def adopt(self, db, patch_id, msgid):
        """Sync a patch the store does not know, when its message is in notmuch."""
        try:
            db.find(msgid)
        except LookupError:
            return
        try:
            patch = self.client.patch(id=patch_id)
        except PwError as e:
            click.echo(f"patch {patch_id} <{msgid}>: {e}")
            return
        self.sync_fetched(db, patch)

    def sync_fetched(self, db, patch, source=None):
        """Sync a patch from its patchwork document."""
        self.sync_patch(db, patch_values(patch), Patch.get_or_none(Patch.id == patch["id"]), source)

    def sync_patch(self, db, remote, row, source=None):
        """Sync a patch from its row as patchwork has it and its stored `row`, if any."""
        try:
            msg = db.find(remote["msgid"])
        except LookupError:
            self.save(remote, tagged=False)
            return

        stored = row_values(row) if row and row.tagged else None
        push, ambiguous = merge(remote, stored, self.tags.values(msg), source)
        label = f"patch {remote['id']} <{remote['msgid']}>"
        for field, values in ambiguous.items():
            click.echo(f"{label}: {field} tagged {sorted(values, key=str)} - taking patchwork's")
        if ambiguous and source == "notmuch":
            return
        if push and not self.push(label, remote, push):
            return

        agreed = remote | push
        self.tags.set(msg, agreed)
        if agreed != stored:
            self.save(agreed, tagged=True)

    def save(self, values, tagged):
        """Store a patch's values; its delegate was recorded as a user when read or pushed."""
        delegate = values["delegate"] and User.get(User.username == values["delegate"])
        Patch.replace(
            project=self.project, tagged=tagged, **values | {"delegate": delegate}
        ).execute()

    def push(self, label, remote, push):
        """Write the values pushed to patchwork, telling whether it took them."""
        changes = ", ".join(f"{field} {remote[field]} -> {value}" for field, value in push.items())
        click.echo(f"{label}: patchwork {changes}")
        try:
            fields = dict(push)
            if push.get("delegate"):
                user = self.user(push["delegate"])
                if user is None:
                    click.echo(f"ERROR no patchwork user {push['delegate']}")
                    return False
                fields["delegate"] = user.id
            self.client.update_patch(id=remote["id"], **fields)
        except PwError as e:
            click.echo(f"ERROR {e}")
            return False
        return True

    def user(self, username):
        """The user with this username, asked of patchwork when not stored; None for no such user.

        Listing users needs a token.
        """
        user = User.get_or_none(User.username == username)
        if user is None:
            users = self.client.users(q=username)
            found = next((u for u in users if u["username"] == username), None)
            if found is not None:
                user = User.create(id=found["id"], username=username)
        return user
