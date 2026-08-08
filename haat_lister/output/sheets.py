"""Optional: push master.csv to a Google Sheet.

Convenience, not a fix, and the last thing built. Everything important already
works without it: `runs/master.csv` is the deliverable, this is a copy of it
somewhere an operator's colleagues can see.

ABSENT AND SILENT WHEN UNCONFIGURED. No credentials, no dependency, no
`--sheets` flag in the help, no warning, no nag. An operator who has never heard
of this should never learn it exists from an error message.

WHAT IT WILL NOT DO:

  * invent a spreadsheet. It writes to a sheet id you supply, in a document
    your service account has already been granted access to, because silently
    creating documents in someone's Drive is not a thing a CSV tool should do.
  * become the source of truth. It replaces the sheet's contents with the
    file's, every time. If someone edits the Google Sheet, the next push
    overwrites their edit -- so the push is explicit and never automatic.
  * carry anything master.csv does not. Same nineteen columns.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..utils.logging import get_logger

log = get_logger(__name__)

# The one scope this needs. Narrow on purpose: `drive` would let it read every
# document the service account can see, and it has no business doing that.
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)

INSTALL_HINT = (
    'Google Sheets export needs its extra:  pip install "haat-lister[sheets]"'
)


class SheetsUnavailable(Exception):
    """Not configured, or the library is not installed. Never raised unless the
    operator explicitly asked for the export."""


@dataclass
class PushResult:
    spreadsheet_id: str
    tab: str
    rows: int
    url: str


def is_configured(settings: Settings) -> bool:
    """Whether `--sheets` could work. Consulted before anything is offered.

    Both halves are required: a credentials file the service account reads, and
    the id of a document it has been given access to. Either alone is not a
    working configuration, and reporting "configured" for half of one would send
    an operator hunting for the wrong problem.
    """
    secrets = settings.secrets
    return bool(secrets.google_credentials_file and secrets.google_sheet_id)


def _client(settings: Settings) -> Any:
    # Imported inside the function, and the ignores are broad because the extra
    # is genuinely optional: with `[sheets]` uninstalled these modules do not
    # exist, and a type checker seeing that is correct rather than wrong.
    try:
        from google.oauth2.service_account import Credentials  # type: ignore[import-not-found]
        from googleapiclient.discovery import build  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SheetsUnavailable(f"{INSTALL_HINT}\n({exc})") from exc

    path = Path(settings.secrets.google_credentials_file)
    if not path.exists():
        raise SheetsUnavailable(
            f"GOOGLE_CREDENTIALS_FILE points at {path}, which does not exist."
        )

    credentials = Credentials.from_service_account_file(str(path), scopes=list(SCOPES))
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def push(sheet_csv: Path, settings: Settings, tab: str = "haat-listings") -> PushResult:
    """Replace the tab's contents with the file's. Explicit, never automatic.

    Raises SheetsUnavailable with something actionable rather than a Google API
    traceback: the three ways this fails in practice are a missing extra, a
    missing credentials file, and a document the service account was never
    shared with, and all three are the operator's to fix.
    """
    if not is_configured(settings):
        raise SheetsUnavailable(
            "Google Sheets export is not configured. Set GOOGLE_CREDENTIALS_FILE to a service "
            "account JSON and GOOGLE_SHEET_ID to a spreadsheet that account has edit access "
            "to. See .env.example."
        )
    if not sheet_csv.exists():
        raise SheetsUnavailable(
            f"There is no sheet to push yet ({sheet_csv} does not exist). Finish a job first."
        )

    with sheet_csv.open("r", encoding="utf-8", newline="") as handle:
        values = [row for row in csv.reader(handle) if row]

    spreadsheet_id = settings.secrets.google_sheet_id
    service = _client(settings)
    api = service.spreadsheets().values()

    try:
        # Clear then write, rather than update in place: a shorter file must not
        # leave rows from a longer one behind, which would silently publish
        # products the operator has removed.
        api.clear(spreadsheetId=spreadsheet_id, range=tab).execute()
        api.update(
            spreadsheetId=spreadsheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
    except Exception as exc:  # noqa: BLE001 -- googleapiclient raises a wide family
        raise SheetsUnavailable(
            f"Google refused the write: {exc}\n"
            f"The usual cause is that the spreadsheet has not been shared with the service "
            f"account's email address, or that no tab is named {tab!r}."
        ) from exc

    return PushResult(
        spreadsheet_id=spreadsheet_id,
        tab=tab,
        rows=max(0, len(values) - 1),
        url=f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
    )
