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
import logging
import os

import click
import notmuch2

from . import Error
from .config import Patchwork
from .patchwork import Client, PwError
from .store import Patch, database, open_store

log = logging.getLogger(__name__)

API_VERSION = "1.3"
# Patches handled while the notmuch database is held open for writing
BATCH = 250

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
ARCHIVED = "archived"
DELEGATE = "delegate-"


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
    "-u",
    "--patchwork-url",
    default="https://patchwork.kernel.org",
    help="The URL to your patchwork instance. Must support REST API.",
)
@click.option(
    "-p",
    "--project",
    required=True,
    help="Patchwork projects to sync, comma separated. "
    "A project=list entry is accepted, the list is not used.",
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
    help="With --patch-id or --msgid: take this side's state, whichever side moved.",
)
@click.option("--debug", is_flag=True, help="Also report patches whose message notmuch lacks.")
def main(
    notmuch_database,
    syncdb,
    patchwork_token,
    patchwork_url,
    project,
    dry_run,
    patch_ids,
    msgids,
    source,
    debug,
):
    """Sync patch state between Patchwork and Notmuch."""
    logging.basicConfig(format="%(message)s")
    if debug:
        log.setLevel(logging.DEBUG)
    if source and not (patch_ids or msgids):
        raise click.UsageError("--from needs --patch-id or --msgid")
    nmdb = os.path.expanduser(notmuch_database)
    api_url = f"{patchwork_url.rstrip('/')}/api/{API_VERSION}"

    try:
        open_store(syncdb, dry_run=dry_run)
        clients = {}
        for entry in project.split(","):
            name = entry.split("=")[0]
            clients[name] = Client(Patchwork(api_url, name), token=patchwork_token, dry_run=dry_run)
            clients[name].project_data()

        with database.atomic() as transaction:
            if patch_ids or msgids:
                patches = find_patches(clients, patch_ids, msgids)
                with open_notmuch(nmdb, dry_run) as db, db.atomic():
                    for name, patch in patches:
                        sync_patch(clients[name], db, name, patch, dry_run, source)
            else:
                for name, client in clients.items():
                    sync_project(client, nmdb, name, dry_run)

            if dry_run:
                transaction.rollback()
    except Error as e:
        raise click.ClickException(str(e)) from e


def open_notmuch(path, dry_run):
    mode = notmuch2.Database.MODE.READ_ONLY if dry_run else notmuch2.Database.MODE.READ_WRITE
    return notmuch2.Database(path, mode=mode)


def find_patches(clients, patch_ids, msgids):
    """Fetch the patches named on the command line from the synced projects holding them."""
    found = []
    for patch_id in patch_ids:
        errors = []
        for project, client in clients.items():
            try:
                found.append((project, client.patch_data(patch_id)))
                break
            except PwError as e:
                errors.append(str(e))
        else:
            raise Error(f"patch {patch_id}: {'; '.join(errors)}")

    for msgid in msgids:
        patches = [
            (project, patch)
            for project, client in clients.items()
            for patch in client.patch_list(msgid=msgid.strip("<>"))
        ]
        if not patches:
            raise Error(f"<{msgid}>: no patch in {', '.join(clients)}")
        found += patches
    return found


def sync_project(client, nmdb, project, dry_run):
    """Sync the project's unarchived patches, then the stored ones patchwork no longer lists."""
    click.echo(f"Looking at project {project}")
    listed = set()
    for batch in itertools.batched(client.patch_list(archived=False), BATCH):
        # We open the DB for each batch as to not hold the notmuch
        # database open blocking other writers for too long.
        with open_notmuch(nmdb, dry_run) as db, db.atomic():
            for patch in batch:
                listed.add(patch["id"])
                sync_patch(client, db, project, patch, dry_run)

    unlisted = [
        row for row in Patch.select().where(Patch.project == project) if row.id not in listed
    ]
    if unlisted:
        with open_notmuch(nmdb, dry_run) as db, db.atomic():
            for row in unlisted:
                sync_unlisted(client, db, project, row, dry_run)

    click.echo(f"Finished processing {len(listed)} {project} patches!")


def sync_unlisted(client, db, project, row, dry_run):
    """Sync a stored patch patchwork no longer lists, taken as archived there.

    Its values are known without asking patchwork until the tags move away from them.
    """
    try:
        msg = db.find(row.msgid)
    except LookupError:
        row.delete_instance()
        return

    stored = stored_values(row)
    if owned_tags(project, msg.tags) != owned_tags(project, project_tags(project, stored)):
        try:
            patch = client.patch_data(row.id)
        except PwError as e:
            click.echo(f"patch {row.id} <{row.msgid}>: {e}")
            return
        sync_patch(client, db, project, patch, dry_run)
    elif not row.archived:
        change_tags(msg, project_tags(project, stored | {"archived": True}), set(), dry_run)
        row.archived = True
        row.save()


def sync_patch(client, db, project, patch, dry_run, source=None):
    """Carry over whichever side moved since the last run, or the `source` side when given.

    Each field is reconciled on its own. Patchwork wins when both sides moved, when the
    patch was never synced, or when the tags give several new values.
    """
    msgid = patch["msgid"][1:-1]
    try:
        msg = db.find(msgid)
    except LookupError:
        log.debug("MESSAGE NOT FOUND: 'id:%s' - skipping", msgid)
        Patch.delete_by_id(patch["id"])
        return

    row = Patch.get_or_none(Patch.id == patch["id"])
    stored = stored_values(row) if row else None
    remote = patchwork_values(patch)
    local = tagged_values(project, msg.tags)
    keep, push, ambiguous = {}, {}, {}
    for field, pw in remote.items():
        if source == "notmuch":
            base = pw  # any other value the tags hold is pushed
        elif source == "patchwork" or stored is None:
            keep[field] = pw
            continue
        else:
            base = stored[field]
        moved = local[field] - {base}
        if pw != base or not moved:
            keep[field] = pw
        elif len(moved) == 1:
            keep[field] = push[field] = moved.pop()
        else:
            keep[field] = pw
            ambiguous[field] = moved

    label = f"patch {patch['id']} <{msgid}>"
    for field, values in ambiguous.items():
        click.echo(f"{label}: {field} tagged {sorted(values, key=str)} - taking patchwork's")
    if ambiguous and source == "notmuch":
        return
    if push:
        changes = ", ".join(f"{field} {remote[field]} -> {value}" for field, value in push.items())
        click.echo(f"{label}: patchwork {changes}")
        update = dict(push)
        if push.get("delegate"):
            # Patchwork only delegates to the project's maintainers, and wants their id
            ids = {user["username"]: user["id"] for user in client.project_data()["maintainers"]}
            if push["delegate"] not in ids:
                click.echo(f"ERROR {push['delegate']} is not a maintainer of {project}")
                return
            update["delegate"] = ids[push["delegate"]]
        try:
            client.update(patch["id"], **update)
        except PwError as e:
            click.echo(f"ERROR {e} - are you maintainer of {project}?")
            return

    want = project_tags(project, keep)
    change_tags(msg, want, owned_tags(project, msg.tags) - want, dry_run)
    Patch.replace(id=patch["id"], project=project, msgid=msgid, **keep).execute()


def patchwork_values(patch):
    delegate = patch["delegate"]["username"] if patch["delegate"] else None
    return {"state": patch["state"], "archived": patch["archived"], "delegate": delegate}


def stored_values(row):
    return {"state": row.state, "archived": row.archived, "delegate": row.delegate}


def tagged_values(project, tags):
    """The values the message's tags give each field.

    No archived tag means not archived, and no delegate tag no delegate.
    """
    prefix = f"pw-{project}-"
    delegates = {t.removeprefix(prefix + DELEGATE) for t in tags if t.startswith(prefix + DELEGATE)}
    return {
        "state": {s for s in STATES if prefix + s in tags},
        "archived": {prefix + ARCHIVED in tags},
        "delegate": delegates or {None},
    }


def project_tags(project, values):
    """The tags a message carries for a patch of the project with these values."""
    prefix = f"pw-{project}-"
    tags = {"patchwork", f"pw-{project}"}
    if values["state"] in STATES:
        tags.add(prefix + values["state"])
    if values["archived"]:
        tags.add(prefix + ARCHIVED)
    if values["delegate"]:
        tags.add(prefix + DELEGATE + values["delegate"])
    return tags


def owned_tags(project, tags):
    """The tags among `tags` that carry a field of the project's patches."""
    prefix = f"pw-{project}-"
    fixed = {prefix + v for v in [*STATES, ARCHIVED]}
    return {t for t in tags if t in fixed or t.startswith(prefix + DELEGATE)}


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
