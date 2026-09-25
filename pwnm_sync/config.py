"""The settings of a run, and where their defaults come from."""

import configparser
import os
from dataclasses import dataclass
from typing import NamedTuple

import click
import platformdirs

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

    notmuch: str
    states: list[str]
    batch: int
    dry_run: bool


def load_defaults(ctx, param, path):
    """Take the [Defaults] section of the configuration file as the options' defaults."""
    if not os.path.isfile(path):
        click.echo(f"Config file {path} not found!")
        return
    config = configparser.ConfigParser()
    config.read(path)
    ctx.default_map = dict(config.items("Defaults"))
