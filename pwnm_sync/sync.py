"""Syncing a patchwork project with notmuch, from what both sides last agreed on."""

import datetime
import itertools

import click
import notmuch2

from .config import WINDOW
from .patchwork import PwError
from .store import Patch, Project
from .tags import Tags

# The events synced, and the field they change
EVENTS = {"patch-state-changed": "state", "patch-delegated": "delegate"}


def open_notmuch(config):
    modes = notmuch2.Database.MODE
    mode = modes.READ_ONLY if config.dry_run else modes.READ_WRITE
    return notmuch2.Database(config.notmuch, mode=mode)


def username(user):
    return user["username"] if user else None


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
        "delegate": row.delegate,
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
    """Each field's value to keep, the ones to push to patchwork, and the ambiguous ones.

    Each field is reconciled on its own: the side that moved since `stored` wins. Patchwork
    wins when both sides moved, when nothing is stored, or when the tags give several new
    values. `source` takes that side's values, whichever side moved.
    """
    keep, push, ambiguous = {}, {}, {}
    for field, values in tagged.items():
        pw = remote[field]
        if source == "notmuch":
            base = pw  # any other value the tags hold is pushed
        elif source == "patchwork" or stored is None:
            keep[field] = pw
            continue
        else:
            base = stored[field]
        moved = values - {base}
        if pw != base or not moved:
            keep[field] = pw
        elif len(moved) == 1:
            keep[field] = push[field] = moved.pop()
        else:
            keep[field] = pw
            ambiguous[field] = moved
    return keep, push, ambiguous


class ProjectSync:
    """Syncs one patchwork project's patches with their notmuch tags."""

    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.project = client.project_name
        self.tags = Tags(config, client.project_name)
        # Their ids there are profile ids, not the user ids a delegate takes
        self.maintainers = {user["username"] for user in client.project()["maintainers"]}
        # Patchwork user ids by username, None for no such user
        self.user_ids = {}

    def run(self, epoch=None):
        """Sync the stored patches and the ones read since the last run.

        The first run reads the patches since `epoch`, or the last year; a later one reads
        the events and the new patches, and the patches since `epoch` when given.
        """
        click.echo(f"Looking at project {self.project}")
        cursors = Project.get_or_none(Project.name == self.project)
        first = cursors is None
        if first:
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

        rows = {row.id: row for row in Patch.select().where(Patch.project == self.project)}
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
            Patch.replace(project=self.project, tagged=False, **remote).execute()
            return

        stored = row_values(row) if row and row.tagged else None
        keep, push, ambiguous = merge(remote, stored, self.tags.values(msg), source)
        label = f"patch {remote['id']} <{remote['msgid']}>"
        for field, values in ambiguous.items():
            click.echo(f"{label}: {field} tagged {sorted(values, key=str)} - taking patchwork's")
        if ambiguous and source == "notmuch":
            return
        if push and not self.push(label, remote, push):
            return

        self.tags.set(msg, keep)
        agreed = remote | keep
        if agreed != stored:
            Patch.replace(project=self.project, tagged=True, **agreed).execute()

    def push(self, label, remote, push):
        """Write the values pushed to patchwork, telling whether it took them."""
        changes = ", ".join(f"{field} {remote[field]} -> {value}" for field, value in push.items())
        click.echo(f"{label}: patchwork {changes}")
        if push.get("delegate") and push["delegate"] not in self.maintainers:
            click.echo(f"ERROR {push['delegate']} is not a maintainer of {self.project}")
            return False
        try:
            fields = dict(push)
            if push.get("delegate"):
                fields["delegate"] = self.user_id(push["delegate"])
            self.client.update_patch(id=remote["id"], **fields)
        except PwError as e:
            click.echo(f"ERROR {e}")
            return False
        return True

    def user_id(self, username):
        """The patchwork id of the user with this username."""
        if username not in self.user_ids:
            users = self.client.users(q=username)
            self.user_ids[username] = next(
                (user["id"] for user in users if user["username"] == username), None
            )
        if self.user_ids[username] is None:
            raise PwError(f"no patchwork user {username}")
        return self.user_ids[username]
