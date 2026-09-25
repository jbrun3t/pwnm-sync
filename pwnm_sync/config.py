"""Settings shared with the patchwork client."""

from typing import NamedTuple


class Patchwork(NamedTuple):
    """The API root of the patchwork instance (e.g. https://patchwork.kernel.org/api/1.3) and the link name of one project on it."""

    url: str
    project: str
