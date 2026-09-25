# pwnm-sync

See `README.md` for what the tool does.

## Environment

Build your own venv if there is not already one. `notmuch2` is compiled
against the notmuch headers (`libnotmuch-dev`).

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Run both before calling anything done, and have them clean:

```sh
.venv/bin/ruff check .
.venv/bin/ruff format .
```

## Patchwork

Do not hammer patchwork with requests. A full run lists every unarchived patch
of each synced project; when trying things, sync only a few patches with
`--patch-id` or `--msgid`.

## Notmuch

Do not run the tool against the real notmuch database or sync database. Copy
the `.notmuch` directory and point `--notmuch-database` and `--syncdb` at the
copies.
