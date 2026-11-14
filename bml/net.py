"""Shared HTTP: one session, sane headers, honest failures."""

from __future__ import annotations

from urllib.parse import urlsplit

import requests
import requests.auth

from . import config

_SESSION: requests.Session | None = None

USER_AGENT = (
    "balatro-mod-loader/0.2 "
    "(+https://github.com/GoodTimesPM/balatro-mod-loader)"
)


GITHUB_HOSTS = {"github.com", "api.github.com", "codeload.github.com",
                "raw.githubusercontent.com", "objects.githubusercontent.com"}


class _GitHubOnly(requests.auth.AuthBase):
    """Adds the token to requests for GitHub hosts and nothing else.

    A session-wide header would go to every host this session talks to,
    including Thunderstore and whatever server a mod's download link names.
    """

    def __init__(self, token: str):
        self.token = token

    def __call__(self, request):
        host = (urlsplit(request.url).hostname or "").lower()
        if urlsplit(request.url).scheme == "https" and host in GITHUB_HOSTS:
            request.headers["Authorization"] = f"Bearer {self.token}"
        return request


def session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({"User-Agent": USER_AGENT})
        token = config.github_token()
        if token:
            # Applied per request, so only GitHub sees it. requests also drops
            # Authorization when a redirect changes host.
            _SESSION.auth = _GitHubOnly(token)
    return _SESSION


def get_json(url: str, timeout: int | None = None):
    response = session().get(url, timeout=timeout or config.REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def download(url: str, dest, timeout: int | None = None) -> int:
    """Stream a download to `dest`. Returns bytes written.

    Refuses anything past MAX_DOWNLOAD_BYTES.
    """
    written = 0
    with session().get(
        url, stream=True, timeout=timeout or config.DOWNLOAD_TIMEOUT
    ) as response:
        response.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > config.MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"download exceeded {config.MAX_DOWNLOAD_BYTES} bytes: {url}"
                    )
                fh.write(chunk)
    return written
