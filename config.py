"""
ERoute - ER Assistant
Configuration and constants.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- API Keys (loaded from environment variables) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# --- Google Sheets ---
SPREADSHEET_ID = "1xnacTzOjde2rJom3yfjzA7MCmyjuw2euvm7_iXmDhJ8"

# Each sheet GID corresponds to one hospital's resource sheet
SHEET_GIDS = [0, 421897479, 524155579, 649277750, 1404852546]

# Base URL for CSV export (no auth needed — the sheet is publicly shared)
CSV_EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    "/export?format=csv&gid={gid}"
)

# --- Gemini model ---
# Keep this configurable because Gemini model availability changes over time.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# --- All possible hospital resources ---
ALL_RESOURCES = [
    "Emergency Department",
    "ICU",
    "Ventilator",
    "CT Scanner",
    "MRI Scanner",
    "X-Ray",
    "Blood Bank",
    "Emergency Surgery",
    "Cardiology",
    "Neurology",
    "Dialysis",
    "Oxygen Supply",
    "Pediatric Emergency",
    "Obstetrics",
]
