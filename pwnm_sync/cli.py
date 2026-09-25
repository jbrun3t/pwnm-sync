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
import os
import sqlite3
import sys

import notmuch2
from requests_futures.sessions import FuturesSession

from . import Error

all_my_tags = [
    "accepted",
    "superseded",
    "changes-requested",
    "rfc",
    "rejected",
    "new",
    "under-review",
    "not-applicable",
    "deferred",
    "awaiting-upstream",
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

    args = argp.parse_args(remaining_argv)

    pw_token = args.patchwork_token
    nmdb = os.path.expanduser(args.notmuch_database)
    sync_db = args.syncdb

    conn = sqlite3.connect(sync_db)

    conn.execute("""CREATE TABLE IF NOT EXISTS pw_patch_status (
    msgid text,
    project text,
    need_sync bool,
    patchid int unique,
    state text,
    PRIMARY KEY(msgid,project))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS nm_patch_status (
    msgid text,
    project text,
    need_sync bool,
    state text,
    PRIMARY KEY(msgid,project))""")
    conn.commit()

    s = FuturesSession()
    s.headers.update({"Authorization": f"Token {pw_token}"})

    patchwork_url = patchwork_login(s, args.patchwork_url)
    projects = get_projects(s, patchwork_url)

    for project in args.sync.split(","):
        project_name, project_list = project.split("=")
        if project_name not in projects:
            raise Error(f"ERROR couldn't find project '{project_name}'")

        print(f"Looking at project {project_name} (id {projects[project_name]})")
        with notmuch2.Database(nmdb) as db:
            if args.epoch is None:
                oldest_msg = get_oldest_nm_message(db, project_list)
            else:
                oldest_msg = args.epoch

            print(f"Going to look at things post {oldest_msg}")
            populate_nm_patch_status(db, conn, project_name, all_my_tags)

        # we now have a map of project names to IDs, so we can use that.
        process_pw_patches_for_project(
            s, nmdb, conn, patchwork_url, project_name, projects[project_name], oldest_msg
        )

        # We now know:
        # 1) What changed locally (nm_patch_status.need_sync=1)
        # 2) What changed remotely (pw_patch_status.need_sync=1)
        # 3) What has update conflicts (union of 1 and 2)
        # Current algorithm for conflicts is "take patchwork status"

        # This syncs the DB for anything with a update conflict
        #
        # We've already applied the PW status in the loop above.
        conn.execute("BEGIN")
        conn.execute(
            """UPDATE nm_patch_status
        SET need_sync=0
        WHERE
        project=? AND
        msgid in (SELECT msgid FROM pw_patch_status WHERE project=? and need_sync=1)""",
            [project_name, project_name],
        )
        conn.commit()

        # We're now left with need_sync=1 on nm_patch_status for only
        # things we need to update in PW.
        update_patchwork(s, conn, patchwork_url, project_name)

        # Things only updated in PW, ignore them (we've forced state sync above)
        conn.execute(
            "UPDATE pw_patch_status set need_sync=0 WHERE project=? and need_sync=1", [project_name]
        )

        conn.commit()


def get_oldest_nm_message(db, project_list):
    msgs = db.messages(f"to:{project_list}", sort=notmuch2.Database.SORT.OLDEST_FIRST)
    # Naive UTC, as patchwork stores the patch dates its filters compare against
    return datetime.datetime.fromtimestamp(next(msgs).date, datetime.UTC).replace(tzinfo=None)


def insert_nm_patch_status(conn, message_id, project_name, tag):
    conn.execute(
        """INSERT OR REPLACE INTO nm_patch_status
    (msgid, project, state, need_sync)
    VALUES (?,?,?,COALESCE((SELECT 1 FROM nm_patch_status WHERE msgid=? and project=? and state IS NOT ?),
                           (SELECT need_sync FROM nm_patch_status WHERE msgid=? and project=?),
                           0)
    )""",
        (message_id, project_name, tag, message_id, project_name, tag, message_id, project_name),
    )


def populate_nm_patch_status(db, conn, project_name, all_my_tags):
    for t in all_my_tags:
        qstr = f"tag:pw-{project_name} and tag:pw-{project_name}-{t}"
        for m in db.messages(qstr):
            insert_nm_patch_status(conn, m.messageid, project_name, t)
        conn.commit()


def patchwork_login(session, url):
    patchwork_url = url + "/api"
    r = session.get(patchwork_url, stream=False).result()
    if r.status_code != 200:
        raise Error(f"ERROR patchwork API request failed status = {r.status_code}")

    patchwork_url = patchwork_url + "/1.0"
    return patchwork_url


def get_projects(session, patchwork_url):
    url = patchwork_url + "/projects"

    projects = {}
    while True:
        r = session.get(url, params={"per_page": 100}, stream=False).result()
        p = r.json()

        for project in p:
            projects[project["link_name"]] = project["id"]

        if not r.links.get("next"):
            break

        url = r.links["next"]["url"]

    return projects


def process_pw_patches(session, nmdb, conn, project_name, r):
    nr_patches_processed = 0
    not_approved = {}
    done = False
    while not done:
        p = r.result().json()
        # We open the DB for each batch as to not hold the notmuch
        # database open blocking other writers for too long.
        with notmuch2.Database(nmdb, mode=notmuch2.Database.MODE.READ_WRITE) as db:
            # We initiate the async load of the next page now, as we go and make the
            # changes to our local DBs.
            if r.result().links.get("next"):
                r = session.get(r.result().links["next"]["url"], stream=False)
            else:
                # This is the last page.
                done = True

            with db.atomic():
                for patch in p:
                    nr_patches_processed = nr_patches_processed + 1
                    msgid = patch["msgid"][1:-1]
                    conn.execute(
                        """INSERT OR REPLACE INTO pw_patch_status
                    (msgid,project,patchid,state,need_sync)
                    VALUES (?,?,?,?,
                    COALESCE((SELECT 1 FROM pw_patch_status WHERE msgid=? and project=? and state IS NOT ?),
                             (SELECT need_sync FROM pw_patch_status WHERE msgid=? and project=?),
                             0)
                    )""",
                        (
                            msgid,
                            project_name,
                            patch["id"],
                            patch["state"],
                            msgid,
                            project_name,
                            patch["state"],
                            msgid,
                            project_name,
                        ),
                    )

                    try:
                        msg = db.find(msgid)
                    except LookupError:
                        print(f"MESSAGE NOT FOUND: 'id:{msgid}' - skipping")
                        # If we don't have the message, just continue.
                        continue

                    # If we need to update PW, skip setting the tags in nm
                    c = conn.cursor()
                    c.execute(
                        "SELECT state from nm_patch_status WHERE msgid=? AND project=? AND need_sync=1",
                        [msgid, project_name],
                    )
                    curstate = c.fetchone()
                    tag = patch["state"]
                    if curstate:
                        print(f"Going to sync {patch['msgid']} to patchwork for {project_name}")
                        tag = curstate[0]

                    msg.tags.add(f"pw-{project_name}")
                    msg.tags.add("patchwork")
                    for t in all_my_tags:
                        msg.tags.discard(f"pw-{project_name}-{t}")

                    if tag in all_my_tags:
                        msg.tags.add(f"pw-{project_name}-{tag}")
                    else:
                        not_approved[tag] = not_approved.get(tag, 0) + 1
                    insert_nm_patch_status(conn, msgid, project_name, tag)
            conn.commit()

        print(f"Processed {nr_patches_processed} {project_name} patches...")

    print(f"Finished processing {nr_patches_processed} {project_name} patches!")
    print(not_approved)


def process_pw_patches_for_project(
    session, nmdb, conn, patchwork_url, project_name, project_id, oldest_msg
):
    patches_url = patchwork_url + "/patches"

    r = session.get(
        patches_url,
        stream=False,
        params={
            "per_page": 500,
            "since": oldest_msg,
            "project": project_id,
        },
    )

    process_pw_patches(session, nmdb, conn, project_name, r)


def update_patchwork(session, conn, patchwork_url, project_name):
    cur = conn.cursor()
    for row in cur.execute(
        """
    SELECT
      pw_patch_status.patchid AS patchid,
      nm_patch_status.state as state,
      nm_patch_status.msgid as msgid
    FROM nm_patch_status, pw_patch_status
    WHERE pw_patch_status.msgid=nm_patch_status.msgid
      AND pw_patch_status.project=nm_patch_status.project
      AND nm_patch_status.project=? and nm_patch_status.need_sync=1""",
        [project_name],
    ):
        print(f"Updating patch {row[0]} (id:{row[2]}) to {row[1]}")
        session.patch(f"{patchwork_url}/patches/{row[0]}/", json={"state": row[1]}).result()
        r = session.get(f"{patchwork_url}/patches/{row[0]}/")
        p = r.result().json()
        if row[1] == p["state"]:
            conn.execute(
                "UPDATE nm_patch_status SET need_sync=0 WHERE msgid=? AND project=?",
                [row[2], project_name],
            )
        else:
            print(f"ERROR State didn't update for {row[0]} - are you maintainer of {project_name}?")


def main():
    try:
        sync()
    except Error as e:
        print("Error", e)
        sys.exit(1)
