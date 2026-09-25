# pwnm-sync - Sync patch state between Patchwork and Notmuch
# Copyright (C) 2018 Stewart Smith, IBM Corp.
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

import configparser
import itertools
import os

import click
import notmuch2

from . import Error
from .config import Patchwork
from .patchwork import Client, PwError
from .store import Patch, database, open_store

API_VERSION = "1.3"
# Patches handled while the notmuch database is held open for writing
BATCH = 100

# The states patchwork.kernel.org serves; a patch in another state gets no state tag
STATES = [
    "new",
    "under-review",
    "changes-requested",
    "awaiting-upstream",
    "handled-elsewhere",
    "not-applicable",
    "superseded",
    "accepted",
    "rejected",
    "deferred",
    "rfc",
]
# Tagged on the message of a patch patchwork no longer lists
ARCHIVED = "archived"


def load_config(ctx, param, path):
    """Take the [Defaults] section of the configuration file as the options' defaults."""
    if not os.path.isfile(path):
        click.echo(f"Config file {path} not found!")
        return
    config = configparser.ConfigParser()
    config.read(path)
    ctx.default_map = dict(config.items("Defaults"))


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-c",
    "--config",
    default=os.path.expanduser("~/.pwnm-sync.ini"),
    is_eager=True,
    expose_value=False,
    callback=load_config,
    help="Configuration file for pwnm-sync",
)
@click.option(
    "-m",
    "--notmuch-database",
    default=os.path.expanduser("~/Maildir/INBOX"),
    help="The notmuch database to sync",
)
@click.option(
    "-d",
    "--syncdb",
    default=os.path.expanduser("~/.pwnm-sync.db"),
    help="The path to the sqlite3 database that pwnm-sync uses to keep track of the state "
    "of the local notmuch and remote patchwork databases.",
)
@click.option(
    "-t",
    "--patchwork-token",
    help="Your Patchwork API token. Get it from /user/ on your patchwork instance.",
)
@click.option(
    "-p",
    "--patchwork-url",
    default="https://patchwork.ozlabs.org",
    help="The URL to your patchwork instance. Must support REST API.",
)
@click.option(
    "-s",
    "--sync",
    default="skiboot",
    help="Patchwork projects to sync, comma separated. "
    "A project=list entry is accepted, the list is not used.",
)
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    help="Print what would change, without writing to patchwork, notmuch or the syncdb",
)
def main(notmuch_database, syncdb, patchwork_token, patchwork_url, sync, dry_run):
    """Sync patch state between Patchwork and Notmuch."""
    nmdb = os.path.expanduser(notmuch_database)
    api_url = f"{patchwork_url.rstrip('/')}/api/{API_VERSION}"

    try:
        open_store(syncdb, dry_run=dry_run)
        with database.atomic() as transaction:
            for project in sync.split(","):
                project_name = project.split("=")[0]
                client = Client(
                    Patchwork(api_url, project_name),
                    token=patchwork_token,
                    dry_run=dry_run,
                )
                project_id = client.project_data()["id"]

                click.echo(f"Looking at project {project_name} (id {project_id})")
                sync_project(client, nmdb, project_name, dry_run)

            if dry_run:
                transaction.rollback()
    except Error as e:
        raise click.ClickException(str(e)) from e


def sync_project(client, nmdb, project, dry_run):
    """Sync the project's unarchived patches; the others leave the sync, tagged archived."""
    mode = notmuch2.Database.MODE.READ_ONLY if dry_run else notmuch2.Database.MODE.READ_WRITE
    listed = set()
    for batch in itertools.batched(client.patch_list(archived=False), BATCH):
        # We open the DB for each batch as to not hold the notmuch
        # database open blocking other writers for too long.
        with notmuch2.Database(nmdb, mode=mode) as db, db.atomic():
            for patch in batch:
                listed.add(patch["id"])
                sync_patch(client, db, project, patch, dry_run)

    gone = [row for row in Patch.select().where(Patch.project == project) if row.id not in listed]
    if gone:
        with notmuch2.Database(nmdb, mode=mode) as db, db.atomic():
            for row in gone:
                click.echo(f"patch {row.id}: leaves the sync")
                try:
                    msg = db.find(row.msgid)
                except LookupError:
                    pass
                else:
                    change_tags(msg, {f"pw-{project}-{ARCHIVED}"}, set(), dry_run)
                row.delete_instance()

    click.echo(f"Finished processing {len(listed)} {project} patches!")


def sync_patch(client, db, project, patch, dry_run):
    """Carry over whichever side moved since the last run.

    Patchwork wins when both did, or when the local state tags are ambiguous.
    """
    msgid = patch["msgid"][1:-1]
    try:
        msg = db.find(msgid)
    except LookupError:
        click.echo(f"MESSAGE NOT FOUND: 'id:{msgid}' - skipping")
        return

    row = Patch.get_or_none(Patch.id == patch["id"])
    state = patch["state"]
    if row and state == row.state:
        # Patchwork did not move: a state tag other than the agreed one is a local change
        local = {s for s in STATES if f"pw-{project}-{s}" in msg.tags} - {row.state}
        if len(local) > 1:
            click.echo(
                f"patch {patch['id']} <{msgid}>: tagged {sorted(local)} - taking patchwork's"
            )
        elif local:
            state = local.pop()
            click.echo(f"patch {patch['id']} <{msgid}>: patchwork {row.state} -> {state}")
            try:
                client.update(patch["id"], state=state)
            except PwError as e:
                click.echo(f"ERROR {e} - are you maintainer of {project}?")
                return

    retag(msg, project, state, patch["archived"], dry_run)
    Patch.replace(id=patch["id"], project=project, msgid=msgid, state=state).execute()


def retag(msg, project, state, archived, dry_run):
    """Set the message's tags for the project to the patch's state."""
    want = {"patchwork", f"pw-{project}"}
    if state in STATES:
        want.add(f"pw-{project}-{state}")
    if archived:
        want.add(f"pw-{project}-{ARCHIVED}")
    ours = {f"pw-{project}-{s}" for s in [*STATES, ARCHIVED]}
    change_tags(msg, want, ours - want, dry_run)


def change_tags(msg, add, remove, dry_run):
    tags = set(msg.tags)
    add = add - tags
    remove = remove & tags
    if not add and not remove:
        return

    changes = [f"+{t}" for t in sorted(add)] + [f"-{t}" for t in sorted(remove)]
    click.echo(f"<{msg.messageid}>: notmuch {' '.join(changes)}")
    if dry_run:
        return
    for t in add:
        msg.tags.add(t)
    for t in remove:
        msg.tags.discard(t)
