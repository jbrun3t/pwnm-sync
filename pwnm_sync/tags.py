"""The notmuch tags carrying the patchwork values of a project's patches."""

import click


class Tags:
    """Reads and writes one project's tags, under the user's aliases."""

    def __init__(self, config, project):
        self.config = config
        # The tag telling a message is a patch of the project, which names its other tags
        self.prefix = config.prefixes.get(project, f"pw-{project}")
        # A delegate tag is this, followed by the username
        self.delegate = f"{self.prefix}-delegate-"
        # [Aliases] reversed: each new name to its old one, and each old name to None, a tag
        # left under its old name being the user's
        self.to_patchwork = {old: None for old in config.aliases} | {
            new: old for old, new in config.aliases.items()
        }

    def patchwork_to_notmuch(self, tag):
        """The tag under its [Aliases] name, if renamed."""
        return self.config.aliases.get(tag, tag)

    def notmuch_to_patchwork(self, tag):
        """The tag under its name before [Aliases], None for one left under an old name."""
        return self.to_patchwork.get(tag, tag)

    def state_to_tag(self, state):
        """The tag of a state, None for a state that gets no tag."""
        if state not in self.config.states:
            return None
        return self.patchwork_to_notmuch(f"{self.prefix}-{state}")

    def tag_to_state(self, tag):
        """The state a tag gives, None for any other tag."""
        return next((s for s in self.config.states if self.state_to_tag(s) == tag), None)

    def delegate_to_tag(self, username):
        """The tag of a delegate, None for no delegate."""
        return self.patchwork_to_notmuch(self.delegate + username) if username else None

    def tag_to_delegate(self, tag):
        """The username a delegate tag gives, None for any other tag."""
        name = self.notmuch_to_patchwork(tag)
        if name and name.startswith(self.delegate):
            return name.removeprefix(self.delegate)
        return None

    def values(self, msg):
        """The values the message's tags give each field; no delegate tag means no delegate."""
        return {
            "state": {s for t in msg.tags if (s := self.tag_to_state(t))},
            "delegate": {d for t in msg.tags if (d := self.tag_to_delegate(t))} or {None},
        }

    def set(self, msg, values):
        """Tag the message with these values, replacing the ones it holds."""
        tags = set(msg.tags)
        want = {
            self.patchwork_to_notmuch("patchwork"),
            self.patchwork_to_notmuch(self.prefix),
            self.state_to_tag(values["state"]),
            self.delegate_to_tag(values["delegate"]),
        } - {None}
        stale = {t for t in tags - want if self.tag_to_state(t) or self.tag_to_delegate(t)}

        add, remove = sorted(want - tags), sorted(stale)
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
