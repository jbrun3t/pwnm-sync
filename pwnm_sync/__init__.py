"""Sync patch state between Patchwork and Notmuch."""

from importlib.metadata import version

# What the tool calls itself, and how it introduces itself to patchwork.
NAME = __package__.replace("_", "-")
VERSION = version(NAME)


class Error(Exception):
    """A failure the tool can name."""
