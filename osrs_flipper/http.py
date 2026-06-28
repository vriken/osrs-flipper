"""Shared HTTP session with retry logic and the required custom User-Agent."""

from __future__ import annotations

from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import USER_AGENT

_session: Session | None = None


def get_session() -> Session:
    """Return a singleton requests.Session with retries and the Wiki-required UA."""
    global _session
    if _session is None:
        _session = Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry)
        _session.mount("https://", adapter)
        _session.mount("http://", adapter)
        _session.headers["User-Agent"] = USER_AGENT
    return _session
