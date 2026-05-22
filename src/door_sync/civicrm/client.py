"""CiviCRM API4 client for door-sync.

Reads active members (contacts with a non-empty card_id and an active
membership) from a WordPress-hosted CiviCRM instance. Read-only — no write
operations. Hand-rolled retry on 5xx and 429.

This module is not pure (it does HTTP), but it does NOT call sys.exit.
Errors surface as CivicrmClientError so the scheduler's per-cycle try/except
can log and continue.
"""

from types import TracebackType

import httpx

from door_sync.config import CivicrmConfig
from door_sync.models import CiviMember

_API_PATH = "/wp-json/civicrm/v3/api4"


class CivicrmClientError(Exception):
    """Raised on non-recoverable CiviCRM API failure."""


class CivicrmClient:
    """Read-only CiviCRM API4 client.

    Construct one per reconcile cycle; the underlying httpx.Client owns
    connection state. Use as a context manager, or call close() explicitly.
    """

    def __init__(self, config: CivicrmConfig) -> None:
        self._config = config
        self._http = httpx.Client(
            base_url=config.host,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            verify=True,
            headers={"Authorization": f"Bearer {config.api_key}"},
        )

    def fetch_active(self) -> list[CiviMember]:
        raise NotImplementedError("implemented in Task 3")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "CivicrmClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
