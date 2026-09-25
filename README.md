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

The instance defaults to patchwork.kernel.org (`patchwork_url` otherwise), and
the notmuch database to the one notmuch is configured with. The token, from
your patchwork `/user/` page (or `PWNM_SYNC_TOKEN` in the environment), is only
needed to write to patchwork, which only accepts writes from the project
maintainers. Only the file sets `states`, the patch states that get a tag,
patchwork.kernel.org's by default. The sync database defaults to
`~/.local/state/pwnm-sync/pwnm-sync.db`; both paths follow the XDG variables.

## Tags

| Tag | Meaning |
|---|---|
| `patchwork`, `pw-{project}` | the message is a patch in the project |
| `pw-{project}-{state}` | its state, if among the configured `states` |
| `pw-{project}-archived` | archived |
| `pw-{project}-delegate-{user}` | delegated to that maintainer |

An optional `[Aliases]` section of the configuration file renames tags:

```
[Aliases]
pw-linux-foo = foo
pw-linux-foo-accepted = foo/applied
```

Only the alias is synced then; a tag left under its old name is yours
(`notmuch tag +foo -pw-linux-foo -- tag:pw-linux-foo` renames it).

## Sync

Each run lists the unarchived patches of each project. For each field (state,
archived, delegate), the sync database keeps the value both sides last agreed
on:

- changed in patchwork: the tags follow
- changed in notmuch: patchwork is updated
- changed on both sides, or tags naming several new values: patchwork wins

Adding a tag without removing the old one counts as a change. A removed state
tag is put back.

Archived patches stay synced while their message is in notmuch; patchwork is
only asked about them once their tags change. Patches without a message in
notmuch are skipped.

`--dry-run` prints the changes and writes nothing. `--patch-id` and `--msgid`
sync only those patches; `--from patchwork|notmuch` forces the direction.
`--epoch DATE` lists the patches since DATE, archived or not, to tag an older
notmuch database.

If the sync database was written by another version, delete it: the next run
rebuilds it, taking patchwork's values.

## License

GPLv3+, see `LICENSE`. `pwnm_sync/patchwork.py` is MIT.
