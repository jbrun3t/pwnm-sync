# pwnm-sync

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

Options default from the `[Defaults]` section of `~/.pwnm-sync.ini`:

```
[Defaults]
notmuch_database = ~/.mail
project = linux-clk,linux-amlogic
patchwork_token = abcdef1234567890
```

The instance defaults to patchwork.kernel.org (`patchwork_url` otherwise). The
token, from your patchwork `/user/` page, is only needed to write to patchwork,
which only accepts writes from the project maintainers.

## Tags

| Tag | Meaning |
|---|---|
| `patchwork`, `pw-{project}` | the message is a patch in the project |
| `pw-{project}-{state}` | its state, among patchwork.kernel.org's |
| `pw-{project}-archived` | archived |
| `pw-{project}-delegate-{user}` | delegated to that maintainer |

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
notmuch are skipped (`--debug` lists them).

`--dry-run` prints the changes and writes nothing. `--patch-id` and `--msgid`
sync only those patches; `--from patchwork|notmuch` forces the direction.
`--epoch DATE` lists the patches since DATE, archived or not, to tag an older
notmuch database.

If the sync database was written by another version, delete it: the next run
rebuilds it, taking patchwork's values.

## License

GPLv3+, see `LICENSE`. `pwnm_sync/patchwork.py` is MIT.

## Contributing

Send patches to stewart@flamingspork.com or use a GitHub pull request.
Contributions must follow the [Developer Certificate of
Origin](https://developercertificate.org/).
