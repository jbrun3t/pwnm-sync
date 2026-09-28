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

Do not hammer patchwork with requests. The first run of a project lists a year
of its patches; when trying things, give `--epoch` a recent date on a fresh
sync database, or sync only a few patches with `--patch-id` or `--msgid`.

## Notmuch

Do not run the tool against the real notmuch database or sync database. Copy
the `.notmuch` directory and point `--notmuch-database` and `--syncdb` at the
copies. notmuch2 refuses to open a database without a notmuch configuration:
when pointing `XDG_CONFIG_HOME` elsewhere, set `NOTMUCH_CONFIG` to the real one.
