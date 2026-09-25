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

import itertools
import os

import click
import notmuch2

from . import Error
from .config import BATCH, CONFIG_FILE, STATES, SYNCDB, Config, Patchwork, load_defaults
from .patchwork import Client, PwError
from .store import Patch, database, open_store

API_VERSION = "1.3"
ARCHIVED = "archived"
DELEGATE = "delegate-"


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
    default=os.path.expanduser("~/Maildir/INBOX"),
    help="The notmuch database to sync",
)
@click.option(
    "-d",
    "--syncdb",
    default=SYNCDB,
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
    "--batch",
    type=click.IntRange(min=1),
    default=BATCH,
    help="Patches handled while the notmuch database is held open for writing.",
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
@click.option(
    "-e",
    "--epoch",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="List the patches since this date, archived or not, rather than every unarchived one.",
)
@click.pass_context
def main(
    ctx,
    notmuch_database,
    syncdb,
    patchwork_token,
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
    api_url = f"{patchwork_url.rstrip('/')}/api/{API_VERSION}"
    # Only the configuration file sets the states
    states = (ctx.default_map or {}).get("states")
    sync = Sync(
        Config(
            notmuch=os.path.expanduser(notmuch_database),
            states=[s.strip() for s in states.split(",") if s.strip()] if states else STATES,
            batch=batch,
            dry_run=dry_run,
        )
    )

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
                with sync.open_notmuch() as db, db.atomic():
                    for name, patch in patches:
                        sync.sync_patch(clients[name], db, name, patch, source)
            else:
                for name, client in clients.items():
                    sync.sync_project(client, name, epoch and epoch.astimezone())

            if dry_run:
                transaction.rollback()
    except Error as e:
        raise click.ClickException(str(e)) from e


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


def patchwork_values(patch):
    delegate = patch["delegate"]["username"] if patch["delegate"] else None
    return {"state": patch["state"], "archived": patch["archived"], "delegate": delegate}


def stored_values(row):
    return {"state": row.state, "archived": row.archived, "delegate": row.delegate}


class Sync:
    """Syncs patches between patchwork and notmuch as the configuration says."""

    def __init__(self, config):
        self.config = config

    def open_notmuch(self):
        modes = notmuch2.Database.MODE
        mode = modes.READ_ONLY if self.config.dry_run else modes.READ_WRITE
        return notmuch2.Database(self.config.notmuch, mode=mode)

    def sync_project(self, client, project, epoch=None):
        """Sync the project's unarchived patches, or all since `epoch`, then the stored ones not listed."""
        click.echo(f"Looking at project {project}")
        listed = set()
        patches = client.patch_list(since=epoch) if epoch else client.patch_list(archived=False)
        for batch in itertools.batched(patches, self.config.batch):
            # We open the DB for each batch as to not hold the notmuch
            # database open blocking other writers for too long.
            with self.open_notmuch() as db, db.atomic():
                for patch in batch:
                    listed.add(patch["id"])
                    self.sync_patch(client, db, project, patch)

        unlisted = [
            row for row in Patch.select().where(Patch.project == project) if row.id not in listed
        ]
        if unlisted:
            with self.open_notmuch() as db, db.atomic():
                for row in unlisted:
                    self.sync_unlisted(client, db, project, row, archived=not epoch)

        click.echo(f"Finished processing {len(listed)} {project} patches!")

    def sync_unlisted(self, client, db, project, row, archived=True):
        """Sync a stored patch missing from the listing.

        Missing means archived in patchwork when `archived` is true; a listing by date also
        misses older patches. Its values are known without asking patchwork until the tags
        move away from them.
        """
        try:
            msg = db.find(row.msgid)
        except LookupError:
            row.delete_instance()
            return

        stored = stored_values(row)
        expected = self.owned_tags(project, self.project_tags(project, stored))
        if self.owned_tags(project, msg.tags) != expected:
            try:
                patch = client.patch_data(row.id)
            except PwError as e:
                click.echo(f"patch {row.id} <{row.msgid}>: {e}")
                return
            self.sync_patch(client, db, project, patch)
        elif archived and not row.archived:
            self.change_tags(msg, self.project_tags(project, stored | {"archived": True}), set())
            row.archived = True
            row.save()

    def sync_patch(self, client, db, project, patch, source=None):
        """Carry over whichever side moved since the last run, or the `source` side when given.

        Each field is reconciled on its own. Patchwork wins when both sides moved, when the
        patch was never synced, or when the tags give several new values.
        """
        msgid = patch["msgid"][1:-1]
        try:
            msg = db.find(msgid)
        except LookupError:
            Patch.delete_by_id(patch["id"])
            return

        row = Patch.get_or_none(Patch.id == patch["id"])
        stored = stored_values(row) if row else None
        remote = patchwork_values(patch)
        local = self.tagged_values(project, msg.tags)
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
            changes = ", ".join(
                f"{field} {remote[field]} -> {value}" for field, value in push.items()
            )
            click.echo(f"{label}: patchwork {changes}")
            update = dict(push)
            if push.get("delegate"):
                # Patchwork only delegates to the project's maintainers, and wants their id
                ids = {
                    user["username"]: user["id"] for user in client.project_data()["maintainers"]
                }
                if push["delegate"] not in ids:
                    click.echo(f"ERROR {push['delegate']} is not a maintainer of {project}")
                    return
                update["delegate"] = ids[push["delegate"]]
            try:
                client.update(patch["id"], **update)
            except PwError as e:
                click.echo(f"ERROR {e} - are you maintainer of {project}?")
                return

        want = self.project_tags(project, keep)
        self.change_tags(msg, want, self.owned_tags(project, msg.tags) - want)
        Patch.replace(id=patch["id"], project=project, msgid=msgid, **keep).execute()

    def tagged_values(self, project, tags):
        """The values the message's tags give each field.

        No archived tag means not archived, and no delegate tag no delegate.
        """
        prefix = f"pw-{project}-"
        delegates = {
            t.removeprefix(prefix + DELEGATE) for t in tags if t.startswith(prefix + DELEGATE)
        }
        return {
            "state": {s for s in self.config.states if prefix + s in tags},
            "archived": {prefix + ARCHIVED in tags},
            "delegate": delegates or {None},
        }

    def project_tags(self, project, values):
        """The tags a message carries for a patch of the project with these values."""
        prefix = f"pw-{project}-"
        tags = {"patchwork", f"pw-{project}"}
        if values["state"] in self.config.states:
            tags.add(prefix + values["state"])
        if values["archived"]:
            tags.add(prefix + ARCHIVED)
        if values["delegate"]:
            tags.add(prefix + DELEGATE + values["delegate"])
        return tags

    def owned_tags(self, project, tags):
        """The tags among `tags` that carry a field of the project's patches."""
        prefix = f"pw-{project}-"
        fixed = {prefix + v for v in [*self.config.states, ARCHIVED]}
        return {t for t in tags if t in fixed or t.startswith(prefix + DELEGATE)}

    def change_tags(self, msg, add, remove):
        tags = set(msg.tags)
        add = add - tags
        remove = remove & tags
        if not add and not remove:
            return

        changes = [f"+{t}" for t in sorted(add)] + [f"-{t}" for t in sorted(remove)]
        click.echo(f"<{msg.messageid}>: notmuch {' '.join(changes)}")
        if self.config.dry_run:
            return
        for t in add:
            msg.tags.add(t)
        for t in remove:
            msg.tags.discard(t)
