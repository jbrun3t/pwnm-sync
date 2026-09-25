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

import argparse
import configparser
import datetime
import itertools
import os
import sys

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


def sync():
    initial_argp = argparse.ArgumentParser(add_help=False)
    initial_argp.add_argument(
        "-c",
        "--config",
        dest="config_file",
        type=str,
        help="Configuration file for pwnm-sync",
        default=os.path.join(os.path.expanduser("~"), ".pwnm-sync.ini"),
    )

    args, remaining_argv = initial_argp.parse_known_args()
    argp = argparse.ArgumentParser()

    defaults = {
        "notmuch_database": os.path.join(os.path.expanduser("~"), "Maildir", "INBOX"),
        "syncdb": os.path.join(os.path.expanduser("~"), ".pwnm-sync.db"),
        "patchwork_url": "https://patchwork.ozlabs.org",
        "sync": "skiboot=skiboot@lists.ozlabs.org",
        "epoch": None,
    }

    if not os.path.isfile(args.config_file):
        print(f"Config file {args.config_file} not found!")
        args.config_file = None

    if args.config_file:
        config = configparser.ConfigParser()
        config.read([args.config_file])
        config_values = dict(config.items("Defaults"))
        defaults = {**defaults, **config_values}

    argp.set_defaults(**defaults)
    argp.add_argument(
        "-m",
        "--notmuch-database",
        dest="notmuch_database",
        type=str,
        help="The notmuch database to sync",
    )
    argp.add_argument(
        "-d",
        "--syncdb",
        dest="syncdb",
        type=str,
        help="The path to the sqlite3 database that pwnm-sync "
        + "uses to keep track of the state of the local notmuch and "
        + "remote patchwork databases.",
    )
    argp.add_argument(
        "-t",
        "--patchwork-token",
        dest="patchwork_token",
        type=str,
        help="Your Patchwork API token. Get it from /user/ on your " + "patchwork instance.",
    )
    argp.add_argument(
        "-p",
        "--patchwork-url",
        dest="patchwork_url",
        type=str,
        help="The URL to your patchwork instance. Must support REST API.",
    )
    argp.add_argument(
        "-s",
        "--sync",
        dest="sync",
        type=str,
        help="Projects and lists to sync. "
        + "In the format project1=list1@server1,project2=list2@server2",
    )
    argp.add_argument(
        "-e",
        "--epoch",
        dest="epoch",
        type=lambda s: (
            datetime.datetime.strptime(s, "%Y-%m-%d").astimezone(datetime.UTC).replace(tzinfo=None)
        ),
        help="Only consider patches on or after this date",
    )
    argp.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print what would change, without writing to patchwork, notmuch or the syncdb",
    )

    args = argp.parse_args(remaining_argv)

    nmdb = os.path.expanduser(args.notmuch_database)
    api_url = f"{args.patchwork_url.rstrip('/')}/api/{API_VERSION}"

    open_store(args.syncdb, dry_run=args.dry_run)
    with database.atomic() as transaction:
        for project in args.sync.split(","):
            project_name, project_list = project.split("=")
            client = Client(
                Patchwork(api_url, project_name),
                token=args.patchwork_token,
                dry_run=args.dry_run,
            )
            project_id = client.project_data()["id"]

            print(f"Looking at project {project_name} (id {project_id})")
            if args.epoch is None:
                with notmuch2.Database(nmdb) as db:
                    oldest_msg = get_oldest_nm_message(db, project_list)
            else:
                oldest_msg = args.epoch

            print(f"Going to look at things post {oldest_msg}")
            sync_project(client, nmdb, project_name, oldest_msg, args.dry_run)

        if args.dry_run:
            transaction.rollback()


def get_oldest_nm_message(db, project_list):
    msgs = db.messages(f"to:{project_list}", sort=notmuch2.Database.SORT.OLDEST_FIRST)
    # Naive UTC, as patchwork stores the patch dates its filters compare against
    return datetime.datetime.fromtimestamp(next(msgs).date, datetime.UTC).replace(tzinfo=None)


def sync_project(client, nmdb, project, since, dry_run):
    """Sync the project's unarchived patches; the others leave the sync."""
    mode = notmuch2.Database.MODE.READ_ONLY if dry_run else notmuch2.Database.MODE.READ_WRITE
    listed = set()
    for batch in itertools.batched(client.patch_list(since=since, archived=False), BATCH):
        # We open the DB for each batch as to not hold the notmuch
        # database open blocking other writers for too long.
        with notmuch2.Database(nmdb, mode=mode) as db, db.atomic():
            for patch in batch:
                listed.add(patch["id"])
                sync_patch(client, db, project, patch, dry_run)

    for row in Patch.select().where(Patch.project == project):
        if row.id not in listed:
            print(f"patch {row.id}: leaves the sync")
            row.delete_instance()

    print(f"Finished processing {len(listed)} {project} patches!")


def sync_patch(client, db, project, patch, dry_run):
    """Carry over whichever side moved since the last run.

    Patchwork wins when both did, or when the local state tags are ambiguous.
    """
    msgid = patch["msgid"][1:-1]
    try:
        msg = db.find(msgid)
    except LookupError:
        print(f"MESSAGE NOT FOUND: 'id:{msgid}' - skipping")
        return

    row = Patch.get_or_none(Patch.id == patch["id"])
    state = patch["state"]
    if row and state == row.state:
        # Patchwork did not move: a state tag other than the agreed one is a local change
        local = {s for s in STATES if f"pw-{project}-{s}" in msg.tags} - {row.state}
        if len(local) > 1:
            print(f"patch {patch['id']} <{msgid}>: tagged {sorted(local)} - taking patchwork's")
        elif local:
            state = local.pop()
            print(f"patch {patch['id']} <{msgid}>: patchwork {row.state} -> {state}")
            try:
                client.update(patch["id"], state=state)
            except PwError as e:
                print(f"ERROR {e} - are you maintainer of {project}?")
                return

    retag(msg, project, state, dry_run)
    Patch.replace(id=patch["id"], project=project, state=state).execute()


def retag(msg, project, state, dry_run):
    tags = set(msg.tags)
    want = {"patchwork", f"pw-{project}"}
    if state in STATES:
        want.add(f"pw-{project}-{state}")
    add = want - tags
    remove = ({f"pw-{project}-{s}" for s in STATES} - want) & tags
    if not add and not remove:
        return

    changes = [f"+{t}" for t in sorted(add)] + [f"-{t}" for t in sorted(remove)]
    print(f"<{msg.messageid}>: notmuch {' '.join(changes)}")
    if dry_run:
        return
    for t in add:
        msg.tags.add(t)
    for t in remove:
        msg.tags.discard(t)


def main():
    try:
        sync()
    except Error as e:
        print("Error", e)
        sys.exit(1)
