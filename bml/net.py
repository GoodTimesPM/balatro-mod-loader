"""Shared HTTP: one session, sane headers, honest failures."""

from __future__ import annotations

import requests

from . import config

_SESSION: requests.Session | None = None

USER_AGENT = (
    "balatro-mod-loader/0.2 "
    "(+https://github.com/GoodTimesPM/balatro-mod-loader)"
)


def session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({"User-Agent": USER_AGENT})
        token = config.github_token()
        if token:
            # Only sent to github.com/codeload; requests strips auth headers
            # on cross-host redirects, which is the behaviour we want.
            _SESSION.headers.update({"Authorization": f"Bearer {token}"})
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
