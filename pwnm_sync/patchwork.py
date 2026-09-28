# SPDX-License-Identifier: MIT
"""Read and update a Patchwork instance over its REST API.

See https://patchwork.readthedocs.io/en/stable/api/rest/
"""

from __future__ import annotations

import datetime
import itertools
from collections.abc import Iterator
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from . import NAME, VERSION, Error
from .config import Patchwork

RETRY = Retry(connect=3, backoff_factor=0.5)
USER_AGENT = f"{NAME}/{VERSION}"
DEFAULT_PER_PAGE = 100
DEFAULT_TIMEOUT = 30
# Leaves the delegate as it is, None clearing it
KEEP = object()


class PwError(Error):
    """The patchwork instance could not be reached, or answered badly."""


class Client:
    """A session against one patchwork instance."""

    def __init__(
        self,
        config: Patchwork,
        *,
        token: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._dry_run = dry_run
        self._config = config
        self._list_params = {"per_page": DEFAULT_PER_PAGE}
        # Patchwork user ids by username, None for no such user
        self._user_ids: dict[str, int | None] = {}

        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._session.mount("https://", HTTPAdapter(max_retries=RETRY))

        if token:
            self._session.headers["Authorization"] = f"Token {token}"

    @staticmethod
    def _decode(response: requests.Response, url: str) -> dict | list:
        try:
            return response.json()
        except ValueError as e:
            raise PwError(f"{url}: response is not JSON: {e}") from e

    @staticmethod
    def _value(value: object) -> object:
        """Spell one query parameter as patchwork expects it: bools lowercase, datetimes as naive UTC."""
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, datetime.datetime):
            if value.tzinfo is not None:
                value = value.astimezone(datetime.UTC).replace(tzinfo=None)
            return value.isoformat()
        return value

    @staticmethod
    def _reason(e: requests.RequestException) -> str:
        """The error, naming the url, then patchwork's explanation when it answered with one."""
        body = e.response.text.strip()[:300] if e.response is not None else ""
        return f"{e}: {body}" if body else str(e)

    def _get(self, url: str) -> requests.Response:
        try:
            response = self._session.get(url, timeout=DEFAULT_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as e:
            raise PwError(self._reason(e)) from e
        return response

    def _list(self, path: str, **params: object) -> Iterator[dict]:
        """Yield every entry of a list endpoint, a page at a time."""
        query = self._list_params | {k: self._value(v) for k, v in params.items() if v is not None}
        url = f"{self._config.url}/{path}/?{urlencode(query)}"
        while url:
            response = self._get(url)
            yield from self._decode(response, url)
            url = response.links.get("next", {}).get("url", "")

    @staticmethod
    def _after(entries: Iterator[dict], cursor: int) -> tuple[list[dict], int]:
        """The entries listed newest first down to id `cursor`, oldest first, and the newest id.

        An entry listed twice, as new ones push the pages down, is taken once.
        """
        found = {e["id"]: e for e in itertools.takewhile(lambda e: e["id"] > cursor, entries)}
        return list(reversed(found.values())), max(found, default=cursor)

    @property
    def project(self) -> str:
        return self._config.project

    def patch_list(self, **params: object) -> Iterator[dict]:
        return self._list("patches", project=self._config.project, **params)

    def event_list(self, **params: object) -> Iterator[dict]:
        """Yield the project's events, newest first."""
        return self._list("events", project=self._config.project, **params)

    def newest_patch_id(self) -> int:
        return next((p["id"] for p in self.patch_list(order="-id", per_page=1)), 0)

    def newest_event_id(self) -> int:
        return next((e["id"] for e in self.event_list(per_page=1)), 0)

    def patches_after(self, patch_id: int) -> tuple[list[dict], int]:
        """The patches after `patch_id`, oldest first, and the newest id."""
        return self._after(self.patch_list(order="-id"), patch_id)

    def events_after(self, event_id: int, **params: object) -> tuple[list[dict], int]:
        """The events after `event_id`, oldest first, and the newest id."""
        return self._after(self.event_list(**params), event_id)

    def _document(self, path: str) -> dict:
        """Fetch one document, refusing one that belongs to another project."""
        url = f"{self._config.url}/{path}/"
        data = self._decode(self._get(url), url)

        found = (data.get("project") or {}).get("link_name")
        if found != self._config.project:
            raise PwError(f"{url}: belongs to project {found!r}, not {self._config.project!r}")
        return data

    def project_data(self) -> dict:
        """Fetch the project, failing when patchwork does not know it: list filters silently match nothing."""
        url = f"{self._config.url}/projects/{self._config.project}/"
        return self._decode(self._get(url), url)

    def patch_data(self, patch_id: int) -> dict:
        return self._document(f"patches/{patch_id}")

    def user_id(self, username: str) -> int:
        """The id of the user with this username; listing users needs a token."""
        if username not in self._user_ids:
            users = self._list("users", q=username)
            self._user_ids[username] = next(
                (user["id"] for user in users if user["username"] == username), None
            )
        if self._user_ids[username] is None:
            raise PwError(f"no patchwork user {username}")
        return self._user_ids[username]

    def update(
        self,
        patch_id: int,
        *,
        state: str | None = None,
        delegate: str | object | None = KEEP,
    ) -> None:
        """Write the settings given on a patch, skipping whatever is None. A dry run writes nothing.

        `delegate` is a username, or None to clear it.
        """
        body = {"state": state} if state is not None else {}
        if delegate is not KEEP:
            body["delegate"] = self.user_id(delegate) if delegate else None
        if not body:
            return
        if self._dry_run:
            return

        url = f"{self._config.url}/patches/{patch_id}/"
        try:
            response = self._session.patch(url, json=body, timeout=DEFAULT_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as e:
            raise PwError(f"cannot update {body}: {self._reason(e)}") from e
