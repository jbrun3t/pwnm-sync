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

Do not hammer patchwork with requests. Every run lists the patches of each
synced project since the epoch; bound it with `--epoch` when trying things.

## Notmuch

Do not run the tool against the real notmuch database or sync database. Copy
the `.notmuch` directory and point `--notmuch-database` and `--syncdb` at the
copies.
