"""The settings of a run, and where their defaults come from."""

import configparser
import datetime
import os
import subprocess
from dataclasses import dataclass
from typing import NamedTuple

import click
import platformdirs
from click.core import ParameterSource

from . import NAME, Error

CONFIG_FILE = platformdirs.user_config_dir(NAME)
SYNCDB = os.path.join(platformdirs.user_state_dir(NAME), f"{NAME}.db")
# Patches handled while the notmuch database is held open for writing, and events read
# per request
BATCH = 250
# How far back the first run lists patches
WINDOW = datetime.timedelta(days=365)
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


class Patchwork(NamedTuple):
    """The API root of the patchwork instance (e.g. https://patchwork.kernel.org/api/1.3) and the link name of one project on it."""

    url: str
    project: str


@dataclass
class Config:
    """How a run treats the notmuch side."""

    notmuch: str | None  # None for the one notmuch is configured with
    states: list[str]
    batch: int
    dry_run: bool
    # Per project, the tag its tags are named after, pw-{project} otherwise
    prefixes: dict[str, str]
    # Our tag names to the ones the user wants instead
    aliases: dict[str, str]

    @classmethod
    def from_context(cls, ctx, *, notmuch, batch, dry_run):
        """The options given, with the settings only the configuration file gives."""
        states = (ctx.default_map or {}).get("states")
        aliases = ctx.meta.get("aliases", {})
        if len(set(aliases.values())) != len(aliases):
            raise click.UsageError("[Aliases] gives the same tag to several of ours")
        return cls(
            notmuch=os.path.expanduser(notmuch) if notmuch else None,
            states=[s.strip() for s in states.split(",") if s.strip()] if states else STATES,
            batch=batch,
            dry_run=dry_run,
            prefixes=ctx.meta.get("prefixes", {}),
            aliases=aliases,
        )


def load_defaults(ctx, param, path):
    """Take the [Defaults] section of the configuration file as the options' defaults.

    The [Prefixes] and [Aliases] sections, naming tags, go to `ctx.meta`.
    """
    if not os.path.isfile(path):
        if ctx.get_parameter_source(param.name) is not ParameterSource.DEFAULT:
            click.echo(f"Config file {path} not found!")
        return
    config = configparser.ConfigParser()
    config.optionxform = str  # tags are case-sensitive
    config.read(path)
    ctx.default_map = dict(config.items("Defaults"))
    for section in ("Prefixes", "Aliases"):
        if config.has_section(section):
            ctx.meta[section.lower()] = dict(config.items(section))


def token_from(ctx, command):
    """The first line a shell command prints, as password managers print a secret.

    An empty command runs patchwork_token_command from the configuration file.
    """
    if not command:
        command = (ctx.default_map or {}).get("patchwork_token_command")
        if not command:
            raise click.UsageError(
                "--with-token-cmd without a command needs patchwork_token_command"
            )
    try:
        run = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip()
        raise Error(f"{command}: exited {e.returncode}" + (f": {stderr}" if stderr else "")) from e
    token = next(iter(run.stdout.splitlines()), "").strip()
    if not token:
        raise Error(f"{command}: printed no token")
    return token
