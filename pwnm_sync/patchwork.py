# SPDX-License-Identifier: MIT
"""Read and update a Patchwork instance over its REST API.

See https://patchwork.readthedocs.io/en/stable/api/rest/
"""

from __future__ import annotations

import datetime
import functools
from collections.abc import Callable, Iterator
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from . import NAME, VERSION, Error

API_VERSION = "1.3"
RETRY = Retry(connect=3, backoff_factor=0.5)
USER_AGENT = f"{NAME}/{VERSION}"
DEFAULT_PER_PAGE = 100
DEFAULT_TIMEOUT = 30


class PwError(Error):
    """The patchwork instance could not be reached, or answered badly."""


def _in_project(fetch: Callable[..., dict]) -> Callable[..., dict]:
    """Refuse a document that belongs to another project than the client's."""

    @functools.wraps(fetch)
    def checked(self: Client, *args: object, **kwargs: object) -> dict:
        data = fetch(self, *args, **kwargs)
        found = (data.get("project") or {}).get("link_name")
        if found != self.project_name:
            raise PwError(f"{data['url']}: belongs to project {found!r}, not {self.project_name!r}")
        return data

    return checked


class Client:
    """A session against one project of a patchwork instance."""

    def __init__(
        self,
        url: str,
        project: str,
        *,
        token: str | None = None,
        dry_run: bool = False,
    ) -> None:
        """`url` is the instance's, e.g. https://patchwork.kernel.org; `project` its link name."""
        self._dry_run = dry_run
        self._url = f"{url.rstrip('/')}/api/{API_VERSION}"
        self._project = project
        self._list_params = {"per_page": DEFAULT_PER_PAGE}

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
        url = f"{self._url}/{path}/?{urlencode(query)}"
        while url:
            response = self._get(url)
            yield from self._decode(response, url)
            url = response.links.get("next", {}).get("url", "")

    def _document(self, path: str) -> dict:
        url = f"{self._url}/{path}/"
        return self._decode(self._get(url), url)

    @property
    def project_name(self) -> str:
        return self._project

    def project(self) -> dict:
        """Fetch the project, failing when patchwork does not know it: list filters silently match nothing."""
        return self._document(f"projects/{self._project}")

    def patches(self, **params: object) -> Iterator[dict]:
        return self._list("patches", project=self._project, **params)

    @_in_project
    def patch(self, *, id: int) -> dict:
        return self._document(f"patches/{id}")

    def events(self, **params: object) -> Iterator[dict]:
        """Yield the project's events, newest first."""
        return self._list("events", project=self._project, **params)

    def users(self, **params: object) -> Iterator[dict]:
        """Yield the instance's users; listing them needs a token."""
        return self._list("users", **params)

    def update_patch(self, *, id: int, **fields: object) -> None:
        """Write these fields on a patch, as patchwork spells them. A dry run writes nothing."""
        if self._dry_run:
            return

        url = f"{self._url}/patches/{id}/"
        try:
            response = self._session.patch(url, json=fields, timeout=DEFAULT_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as e:
            raise PwError(f"cannot update {fields}: {self._reason(e)}") from e
