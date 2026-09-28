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

import os

import click

from . import Error
from .config import BATCH, CONFIG_FILE, SYNCDB, Config, Patchwork, load_defaults, token_from
from .patchwork import Client, PwError
from .store import database, open_store
from .sync import ProjectSync, open_notmuch

API_VERSION = "1.3"


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
    required=True,
    help="Patchwork projects to sync, comma separated.",
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
    help="With --patch-id or --msgid: take this side's state, whichever side moved.",
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
    api_url = f"{patchwork_url.rstrip('/')}/api/{API_VERSION}"
    config = Config.from_context(ctx, notmuch=notmuch_database, batch=batch, dry_run=dry_run)

    try:
        if token_command is not None:
            patchwork_token = token_from(ctx, token_command)
        open_store(os.path.expanduser(syncdb), dry_run=dry_run)
        clients = {
            name: Client(Patchwork(api_url, name), token=patchwork_token, dry_run=dry_run)
            for name in filter(None, (p.strip() for p in project.split(",")))
        }
        syncs = {name: ProjectSync(config, client) for name, client in clients.items()}

        with database.atomic() as transaction:
            if patch_ids or msgids:
                patches = find_patches(clients, patch_ids, msgids)
                with open_notmuch(config) as db, db.atomic():
                    for name, patch in patches:
                        syncs[name].sync_fetched(db, patch, source)
            else:
                for sync in syncs.values():
                    sync.run(epoch and epoch.astimezone())

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
