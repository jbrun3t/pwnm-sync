"""The settings of a run, and where their defaults come from."""

import configparser
import datetime
import os
import subprocess
from dataclasses import dataclass, field

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


@dataclass
class FileSettings:
    """What only the configuration file sets: none of it is an option."""

    states: list[str] = field(default_factory=lambda: list(STATES))
    # Per project, the tag its tags are named after, pw-{project} otherwise
    prefixes: dict[str, str] = field(default_factory=dict)
    # Tag names to the [Aliases] names the user gives them
    aliases: dict[str, str] = field(default_factory=dict)
    token_command: str | None = None


@dataclass
class Config:
    """The settings of a run: the notmuch database, how tags are named, batches, dry run."""

    notmuch: str | None  # None for the one notmuch is configured with
    states: list[str]
    batch: int
    dry_run: bool
    prefixes: dict[str, str]
    aliases: dict[str, str]

    @classmethod
    def from_context(cls, ctx, *, notmuch, batch, dry_run):
        """The options given, with the settings only the configuration file gives."""
        settings = ctx.meta[NAME]
        return cls(
            notmuch=os.path.expanduser(notmuch) if notmuch else None,
            states=settings.states,
            batch=batch,
            dry_run=dry_run,
            prefixes=settings.prefixes,
            aliases=settings.aliases,
        )


def _split(value):
    """The items of a comma separated list, blanks dropped."""
    return [item.strip() for item in value.split(",") if item.strip()]


def load_defaults(ctx, param, path):
    """Take the [Defaults] section of the configuration file as the options' defaults.

    What only the file sets, [Defaults] states and patchwork_token_command, and the
    [Prefixes] and [Aliases] sections, goes to `ctx.meta[NAME]`.
    """
    settings = ctx.meta[NAME] = FileSettings()
    if not os.path.isfile(path):
        if ctx.get_parameter_source(param.name) is not ParameterSource.DEFAULT:
            raise click.FileError(path, hint="no such file")
        return
    config = configparser.ConfigParser()
    config.optionxform = str  # tags are case-sensitive
    try:
        config.read(path)
    except configparser.Error as e:
        raise click.FileError(path, hint=str(e)) from e

    def section(name):
        return dict(config.items(name)) if config.has_section(name) else {}

    defaults = section("Defaults")
    if states := defaults.pop("states", None):
        settings.states = _split(states)
    settings.token_command = defaults.pop("patchwork_token_command", None)
    settings.prefixes = section("Prefixes")
    settings.aliases = section("Aliases")
    if len(set(settings.aliases.values())) != len(settings.aliases):
        raise click.UsageError("[Aliases] gives the same name to several tags")
    ctx.default_map = defaults


def token_from(ctx, command):
    """The first line a shell command prints, as password managers print a secret.

    An empty command runs patchwork_token_command from the configuration file.
    """
    if not command:
        command = ctx.meta[NAME].token_command
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
