"""
ERoute - ER Assistant
Google Sheets service: fetches hospital resource data from the public spreadsheet.
"""

import csv
import io
import logging
from typing import Any

import requests

from config import SPREADSHEET_ID, SHEET_GIDS, CSV_EXPORT_URL

logger = logging.getLogger(__name__)


def _fetch_sheet_csv(gid: int) -> str:
    """Download a single sheet as CSV text."""
    url = CSV_EXPORT_URL.format(spreadsheet_id=SPREADSHEET_ID, gid=gid)
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.text


def _parse_hospital_sheet(csv_text: str) -> dict[str, Any]:
    """
    Parse one hospital sheet into a structured dict.

    Returns
    -------
    {
        "hospital_id": str,
        "name": str,
        "address": str,
        "phone": str,
        "latitude": float,
        "longitude": float,
        "resources": {resource_name: {"available": bool, "notes": str}, ...}
    }
    """
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)

    if len(rows) < 2:
        return {}

    # Hospital metadata is repeated only on the first resource row.
    first_data = next((row for row in rows[1:] if len(row) > 5 and row[5].strip()), [])
    if not first_data:
        return {}

    def parse_coordinate(value: str) -> float:
        try:
            return float(value.strip()) if value.strip() else 0.0
        except ValueError:
            return 0.0

    hospital_info = {
        "hospital_id": first_data[4] if len(first_data) > 4 else "",
        "name": first_data[5] if len(first_data) > 5 else "",
        "address": first_data[6] if len(first_data) > 6 else "",
        "phone": first_data[7] if len(first_data) > 7 else "",
        "latitude": parse_coordinate(first_data[8]) if len(first_data) > 8 else 0.0,
        "longitude": parse_coordinate(first_data[9]) if len(first_data) > 9 else 0.0,
    }

    # All data rows contain resource info in columns A–C
    resources: dict[str, dict[str, Any]] = {}
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        resource_name = row[0].strip()
        available = (
            row[1].strip().upper() in {"TRUE", "YES", "AVAILABLE"}
            if len(row) > 1
            else False
        )
        notes = row[2].strip() if len(row) > 2 else ""
        resources[resource_name] = {"available": available, "notes": notes}

    hospital_info["resources"] = resources
    return hospital_info


def fetch_all_hospitals() -> list[dict[str, Any]]:
    """
    Fetch and parse resource data for every hospital from Google Sheets.

    Returns a list of hospital dicts (see ``_parse_hospital_sheet``).
    """
    hospitals = []
    for gid in SHEET_GIDS:
        try:
            csv_text = _fetch_sheet_csv(gid)
            hospital = _parse_hospital_sheet(csv_text)
            if hospital and hospital.get("name"):
                hospitals.append(hospital)
        except requests.RequestException:
            logger.exception("Failed to fetch hospital sheet gid=%s", gid)
        except (csv.Error, ValueError, TypeError):
            logger.exception("Failed to parse hospital sheet gid=%s", gid)

    if not hospitals:
        raise RuntimeError(
            "No hospital data could be loaded from Google Sheets. "
            "Check the workbook access and sheet configuration."
        )
    return hospitals


def find_hospitals_with_resources(
    hospitals: list[dict[str, Any]],
    required_resources: list[str],
) -> list[dict[str, Any]]:
    """
    Filter hospitals to only those that have **all** required resources available.
    """
    matching = []
    for h in hospitals:
        res = h.get("resources", {})
        has_all = all(
            res.get(r, {}).get("available", False) for r in required_resources
        )
        if has_all:
            matching.append(h)
    return matching
