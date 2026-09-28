"""The notmuch tags carrying the patchwork values of a project's patches."""

import click

DELEGATE = "delegate-"


class Tags:
    """Reads and writes one project's tags, under the user's aliases."""

    def __init__(self, config, project):
        self.config = config
        # The tag telling a message is a patch of the project, which names its other tags
        self.prefix = config.prefixes.get(project, f"pw-{project}")
        self.canonical = {alias: tag for tag, alias in config.aliases.items()}

    def _ours(self, msg):
        """The message's tags under our names; our name for a tag with an alias is a user tag."""
        return {
            self.canonical.get(t, t)
            for t in msg.tags
            if t in self.canonical or t not in self.config.aliases
        }

    def values(self, msg):
        """The values the message's tags give each field; no delegate tag means no delegate."""
        tags = self._ours(msg)
        delegate = f"{self.prefix}-{DELEGATE}"
        delegates = {t.removeprefix(delegate) for t in tags if t.startswith(delegate)}
        return {
            "state": {s for s in self.config.states if f"{self.prefix}-{s}" in tags},
            "delegate": delegates or {None},
        }

    def set(self, msg, values):
        """Tag the message with these values, replacing the ones it holds."""
        tags = self._ours(msg)
        delegate = f"{self.prefix}-{DELEGATE}"
        states = {f"{self.prefix}-{s}" for s in self.config.states}
        want = {"patchwork", self.prefix}
        if values["state"] in self.config.states:
            want.add(f"{self.prefix}-{values['state']}")
        if values["delegate"]:
            want.add(delegate + values["delegate"])
        stale = {t for t in tags - want if t in states or t.startswith(delegate)}

        alias = self.config.aliases
        add = sorted(alias.get(t, t) for t in want - tags)
        remove = sorted(alias.get(t, t) for t in stale)
        if not add and not remove:
            return
        changes = [f"+{t}" for t in add] + [f"-{t}" for t in remove]
        click.echo(f"<{msg.messageid}>: notmuch {' '.join(changes)}")
        if self.config.dry_run:
            return
        for t in add:
            msg.tags.add(t)
        for t in remove:
            msg.tags.discard(t)
