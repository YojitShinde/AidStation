"""
ERoute - ER Assistant
Location service: determines the device's current position so the user never
has to type where they are.

Order of attempts:
  1. ERROUTE_LOCATION override (lat,lng) from the environment, if set.
  2. Windows Location Service (GPS / Wi-Fi positioning) via PowerShell.
  3. IP-based geolocation (city-level, approximate) as a last resort.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import requests

from config import LOCATION_OVERRIDE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceLocation:
    latitude: float
    longitude: float
    accuracy_m: float | None
    source: str  # "override" | "device" | "ip" | "mentioned"

    @property
    def is_precise(self) -> bool:
        return self.source != "ip"

    def label(self) -> str:
        if self.source == "device":
            if self.accuracy_m:
                return f"Device location (±{int(self.accuracy_m)} m)"
            return "Device location"
        if self.source == "override":
            return "Configured location"
        if self.source == "mentioned":
            return "Location from your message"
        return "Approximate location from your network (city-level, may be inaccurate)"


def _valid(lat: float, lng: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lng <= 180 and not (lat == 0 and lng == 0)


def _from_override() -> DeviceLocation | None:
    if not LOCATION_OVERRIDE:
        return None
    try:
        lat_text, lng_text = LOCATION_OVERRIDE.split(",")
        lat, lng = float(lat_text), float(lng_text)
    except ValueError:
        logger.warning("ERROUTE_LOCATION must look like '19.1197,72.8464'")
        return None
    return DeviceLocation(lat, lng, None, "override") if _valid(lat, lng) else None


_WINDOWS_SCRIPT = r"""
Add-Type -AssemblyName System.Device
$w = New-Object System.Device.Location.GeoCoordinateWatcher([System.Device.Location.GeoPositionAccuracy]::High)
[void]$w.TryStart($false, [TimeSpan]::FromSeconds(10))
$deadline = (Get-Date).AddSeconds(10)
while ($w.Status -ne 'Ready' -and $w.Status -ne 'Disabled' -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 200
}
$loc = $w.Position.Location
if ($w.Status -eq 'Ready' -and -not $loc.IsUnknown) {
    $inv = [Globalization.CultureInfo]::InvariantCulture
    Write-Output ($loc.Latitude.ToString($inv) + ',' + $loc.Longitude.ToString($inv) + ',' + $loc.HorizontalAccuracy.ToString($inv))
}
$w.Stop()
"""


def _from_windows() -> DeviceLocation | None:
    """Ask the Windows Location Service (GPS, Wi-Fi, or cell positioning)."""
    if sys.platform != "win32":
        return None
    try:
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", _WINDOWS_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        if not lines:
            logger.info("Windows Location Service returned no fix (disabled or no signal).")
            return None
        lat_text, lng_text, acc_text = lines[-1].split(",")
        lat, lng, acc = float(lat_text), float(lng_text), float(acc_text)
    except (subprocess.SubprocessError, OSError, ValueError):
        logger.exception("Windows location lookup failed")
        return None
    if not _valid(lat, lng):
        return None
    return DeviceLocation(lat, lng, acc if acc == acc else None, "device")


def _from_ip() -> DeviceLocation | None:
    """Coarse fallback: locate by public IP address."""
    for url in ("https://ipapi.co/json/", "https://ipwho.is/"):
        try:
            response = requests.get(url, timeout=6)
            response.raise_for_status()
            data = response.json()
            lat, lng = float(data["latitude"]), float(data["longitude"])
        except (requests.RequestException, KeyError, TypeError, ValueError):
            logger.warning("IP geolocation via %s failed", url)
            continue
        if _valid(lat, lng):
            return DeviceLocation(lat, lng, None, "ip")
    return None


_lock = threading.Lock()
_cached: DeviceLocation | None = None
_cached_at = 0.0


def get_device_location(max_age_s: float = 120.0) -> DeviceLocation | None:
    """
    Return the device's location, or None if it cannot be determined.
    Thread-safe; recent results are reused for ``max_age_s`` seconds.
    """
    global _cached, _cached_at
    with _lock:
        if _cached and time.monotonic() - _cached_at < max_age_s:
            return _cached
        location = _from_override() or _from_windows() or _from_ip()
        if location:
            _cached, _cached_at = location, time.monotonic()
        return location
