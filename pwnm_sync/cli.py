# pwnm-sync - Sync patch state between Patchwork and Notmuch
# Copyright (C) 2018 Stewart Smith, IBM Corp.
# Copyright (C) 2026 Jerome Brunet <jbrunet@baylibre.com>
#
# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier:  GPL-3.0-or-later

"""Sync patch state between Patchwork and Notmuch, from what both sides last agreed on."""

import datetime
import itertools
import os

import click
import notmuch2
import peewee

from . import Error
from .config import BATCH, CONFIG_FILE, SYNCDB, WINDOW, Config, load_defaults, token_from
from .patchwork import Client, PwError
from .store import Patch, Project, User, database, open_store
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
            event = newest_id(self.client.events(per_page=1))
            patch = newest_id(self.client.patches(order="-id", per_page=1))
            cursors = Project(name=self.project, patch=patch, event=event)
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
            self.adopt(unknown)

        cursors.save(force_insert=first)
        click.echo(f"Finished processing {len(remote)} {self.project} patches!")

    def adopt(self, unknown):
        """Sync the patches the store does not know, msgids by id, whose message is in notmuch.

        They are fetched with notmuch open read-only, which holds no other writer back.
        """
        fetched = []
        with notmuch2.Database(self.config.notmuch) as db:
            for patch_id, msgid in unknown.items():
                try:
                    db.find(msgid)
                    fetched.append(self.client.patch(id=patch_id))
                except LookupError:
                    continue
                except PwError as e:
                    click.echo(f"patch {patch_id} <{msgid}>: {e}")
        if fetched:
            with open_notmuch(self.config) as db, db.atomic():
                for patch in fetched:
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


def find_patches(syncs, patch_ids, msgids):
    """Fetch the patches named on the command line, each with the sync of its project."""
    found = []
    for patch_id in patch_ids:
        errors = []
        for sync in syncs.values():
            try:
                found.append((sync, sync.client.patch(id=patch_id)))
                break
            except PwError as e:
                errors.append(str(e))
        else:
            raise Error(f"patch {patch_id}: {'; '.join(errors)}")

    for msgid in msgids:
        patches = [
            (sync, patch)
            for sync in syncs.values()
            for patch in sync.client.patches(msgid=msgid.strip("<>"))
        ]
        if not patches:
            raise Error(f"<{msgid}>: no patch in {', '.join(syncs)}")
        found += patches
    return found


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-c",
    "--config",
    default=CONFIG_FILE,
    is_eager=True,
    expose_value=False,
    callback=load_defaults,
    help="Configuration file for pwnm-sync",
)
@click.option(
    "-m",
    "--notmuch-database",
    help="The notmuch database to sync, the one notmuch is configured with by default.",
)
@click.option(
    "-d",
    "--syncdb",
    default=SYNCDB,
    help="Where pwnm-sync keeps the values patchwork and notmuch last agreed on.",
)
@click.option(
    "-t",
    "--patchwork-token",
    envvar="PWNM_SYNC_TOKEN",
    show_envvar=True,
    help="Your Patchwork API token. Get it from /user/ on your patchwork instance.",
)
@click.option(
    "--with-token-cmd",
    "token_command",
    is_flag=False,
    flag_value="",
    help="Take the token from the first line this shell command prints, "
    "patchwork_token_command in the configuration file when no command is given.",
)
@click.option(
    "-u",
    "--patchwork-url",
    default="https://patchwork.kernel.org",
    help="The patchwork instance, 3.1 or later (REST API 1.3).",
)
@click.option(
    "-p",
    "--project",
    multiple=True,
    required=True,
    help="A patchwork project to sync. Repeatable.",
)
@click.option(
    "--batch",
    type=click.IntRange(min=1),
    default=BATCH,
    help="Patches handled while the notmuch database is held open for writing, "
    "and events read per request (patchwork serves at most 250).",
)
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    help="Print what would change, without writing to patchwork, notmuch or the syncdb",
)
@click.option(
    "--patch-id",
    "patch_ids",
    type=int,
    multiple=True,
    help="Sync only this patchwork patch. Repeatable.",
)
@click.option(
    "--msgid",
    "msgids",
    multiple=True,
    help="Sync only the patches of this message-id. Repeatable.",
)
@click.option(
    "--from",
    "source",
    type=click.Choice(["patchwork", "notmuch"]),
    help="With --patch-id or --msgid: take this side's state and delegate, whichever side moved.",
)
@click.option(
    "-e",
    "--epoch",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="List the patches since this date: on the first run instead of the last year, "
    "on later ones on top of the new patches.",
)
@click.pass_context
def main(
    ctx,
    notmuch_database,
    syncdb,
    patchwork_token,
    token_command,
    patchwork_url,
    project,
    batch,
    dry_run,
    patch_ids,
    msgids,
    source,
    epoch,
):
    """Sync patch state between Patchwork and Notmuch."""
    if source and not (patch_ids or msgids):
        raise click.UsageError("--from needs --patch-id or --msgid")
    if epoch and (patch_ids or msgids):
        raise click.UsageError("--epoch does not apply to --patch-id or --msgid")
    config = Config.from_context(ctx, notmuch=notmuch_database, batch=batch, dry_run=dry_run)

    try:
        if token_command is not None:
            patchwork_token = token_from(ctx, token_command)
        open_store(os.path.expanduser(syncdb), dry_run=dry_run)
        syncs = {
            name: ProjectSync(
                config, Client(patchwork_url, name, token=patchwork_token, dry_run=dry_run)
            )
            for name in project
        }

        with database.atomic() as transaction:
            if patch_ids or msgids:
                patches = find_patches(syncs, patch_ids, msgids)
                with open_notmuch(config) as db, db.atomic():
                    for sync, patch in patches:
                        sync.sync_fetched(db, patch, source)
            else:
                for sync in syncs.values():
                    sync.run(epoch and epoch.astimezone())

            if dry_run:
                transaction.rollback()
    except Error as e:
        raise click.ClickException(str(e)) from e
