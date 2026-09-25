"""The settings of a run, and where their defaults come from."""

import configparser
import os
from dataclasses import dataclass
from typing import NamedTuple

import click
import platformdirs
from click.core import ParameterSource

from . import NAME

CONFIG_FILE = platformdirs.user_config_dir(NAME)
SYNCDB = os.path.join(platformdirs.user_state_dir(NAME), f"{NAME}.db")
# Patches handled while the notmuch database is held open for writing
BATCH = 250
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
    # Our tag names to the ones the user wants instead
    aliases: dict[str, str]


def load_defaults(ctx, param, path):
    """Take the [Defaults] section of the configuration file as the options' defaults.

    The [Aliases] section, renaming tags, goes to `ctx.meta["aliases"]`.
    """
    if not os.path.isfile(path):
        if ctx.get_parameter_source(param.name) is not ParameterSource.DEFAULT:
            click.echo(f"Config file {path} not found!")
        return
    config = configparser.ConfigParser()
    config.optionxform = str  # tags are case-sensitive
    config.read(path)
    ctx.default_map = dict(config.items("Defaults"))
    if config.has_section("Aliases"):
        ctx.meta["aliases"] = dict(config.items("Aliases"))
