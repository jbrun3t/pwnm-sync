# pwnm-sync (v2)

Sync patch state between patchwork and notmuch tags, both ways.

## Install

Needs:

- Python 3.12+
- a patchwork 3.1+ instance (REST API 1.3)
- the notmuch headers (`libnotmuch-dev` on Debian), to build the `notmuch2`
  bindings

```
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pwnm-sync --help
```

## Configure

Every option (see `pwnm-sync --help`) takes its default from the `[Defaults]`
section of `~/.config/pwnm-sync`. `samples/pwnm-sync` lists every setting.

```
[Defaults]
project = linux-foo, linux-bar
patchwork_token = abcdef1234567890
```

- **Projects**: required. Comma separated in the file, or `-p` repeated.
- **Instance**: patchwork.kernel.org, unless `patchwork_url` says otherwise.
- **Token**: only needed to write to patchwork, which only accepts writes from
  the project's maintainers, and only as delegates. Get it from your patchwork
  `/user/` page. Also taken from:
  - `PWNM_SYNC_TOKEN` in the environment
  - `--with-token-cmd`: the first line a command prints (e.g. a password
    manager), the command given or `patchwork_token_command`
- **States**: the patch states that get a tag, patchwork.kernel.org's by
  default. Only settable in the file.
- **Notmuch database**: the one notmuch is configured with.
- **Sync database**: `~/.local/state/pwnm-sync/pwnm-sync.db`.

Both default paths follow the XDG variables.

## Tags

| Tag | Meaning |
|---|---|
| `patchwork`, `pw-{project}` | the message is a patch in the project |
| `pw-{project}-{state}` | its state, if among the configured `states` |
| `pw-{project}-delegate-{user}` | delegated to that maintainer |

Two optional sections of the configuration file rename them:

- `[Prefixes]`: a project's tags use another prefix than `pw-{project}`
- `[Aliases]`: single tags, by their name after the prefix change

```
[Prefixes]
linux-foo = pw-foo

[Aliases]
patchwork = pw
pw-foo-accepted = foo/applied
```

Only the new names are synced; tags left under their old names are yours.

## Sync

- **First run** of a project: lists its patches from the last year, archived
  or not.
- **Later runs**: read what patchwork logged since, i.e. state and delegate
  changes, and new patches. Archiving is not logged, so it is not synced.

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

### Options

- `--dry-run`: print the changes, write nothing. It cannot tell which writes
  patchwork would refuse.
- `--patch-id`, `--msgid`: sync only those patches, of projects synced before.
- `--from patchwork|notmuch`: with the above, force the direction.
- `--epoch DATE`: list the patches since DATE; on the first run instead of the
  last year, afterwards to add older ones.

### Starting over

The sync database keeps every patch it has seen. Delete it to start over, or
when another version wrote it. The next run rebuilds it from patchwork's
values, listing the patches of the last year again, or since `--epoch`.

## License

GPLv3+, see `LICENSE`.
