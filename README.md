# pwnm-sync (v2)

Sync patch state between patchwork and notmuch tags, both ways.

## Install

Needs Python 3.12+, a patchwork 3.1+ instance (REST API 1.3), and the notmuch
headers (`libnotmuch-dev` on Debian) to build the `notmuch2` bindings.

```
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pwnm-sync --help
```

## Configure

Options (see `pwnm-sync --help`) default from the `[Defaults]` section of
`~/.config/pwnm-sync`; `samples/pwnm-sync` lists every setting:

```
[Defaults]
project = linux-blabla
patchwork_token = abcdef1234567890
```

Several projects are synced by repeating `-p`, or comma separated in the file:
`project = linux-foo, linux-bar`. The instance defaults to patchwork.kernel.org
(`patchwork_url` otherwise), and the notmuch database to the one notmuch is
configured with. The token, from your patchwork `/user/` page (or
`PWNM_SYNC_TOKEN` in the environment), is only needed to write to patchwork,
which only accepts writes from the project maintainers, and only them as
delegates. `--with-token-cmd` takes it from what a command prints instead, e.g.
a password manager: the command given, or `patchwork_token_command`. Only the
file sets `states`, the patch states that get a tag, patchwork.kernel.org's by
default. The sync database defaults to `~/.local/state/pwnm-sync/pwnm-sync.db`;
both paths follow the XDG variables.

## Tags

| Tag | Meaning |
|---|---|
| `patchwork`, `pw-{project}` | the message is a patch in the project |
| `pw-{project}-{state}` | its state, if among the configured `states` |
| `pw-{project}-delegate-{user}` | delegated to that maintainer |

In the configuration file, an optional `[Prefixes]` section names a project's
tags after another prefix than `pw-{project}`, and an optional `[Aliases]`
section renames single tags, by their names after the prefix change:

```
[Prefixes]
linux-foo = pw-foo

[Aliases]
patchwork = pw
pw-foo-accepted = foo/applied
```

Only the new names are synced then; tags left under their old names are yours.

## Sync

The first run of a project lists its patches from the last year, archived or
not. Later runs read what patchwork logged since: the state and delegate
changes, and the new patches. Archiving is not logged, so it is not synced.

For each field (state, delegate), the sync database keeps the value both sides
last agreed on:

- changed in patchwork: the tags follow
- changed in notmuch: patchwork is updated
- changed on both sides, or tags naming several new values: patchwork wins

Adding a tag without removing the old one counts as a change. A removed state
tag is put back.

A patch whose message is not in notmuch yet is tagged once it arrives. A change
in patchwork to a patch older than the first listing syncs it too, if its
message is in notmuch.

`--dry-run` prints the changes and writes nothing, so it cannot tell which
patchwork would refuse. `--patch-id` and `--msgid` sync only those patches, of
projects synced before; `--from patchwork|notmuch` forces the direction.
`--epoch DATE` lists the patches since DATE: on the first run instead of the
last year, afterwards to add older ones.

The sync database keeps every patch it has seen. To start over, or when it was
written by another version, delete it: the next run rebuilds it, taking
patchwork's values, and lists the patches of the last year again, or since
`--epoch`.

## License

GPLv3+, see `LICENSE`.
