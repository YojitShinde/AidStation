"""
ERoute - ER Assistant
Google Maps service: driving routes (distance, duration, polyline) between the
user's location and candidate hospitals, plus the styled route map image.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import googlemaps
import requests

from config import GOOGLE_MAPS_API_KEY

logger = logging.getLogger(__name__)

ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
STATIC_MAP_URL = "https://maps.googleapis.com/maps/api/staticmap"
MAX_STATIC_SIDE = 640  # Static Maps limit at scale=1 (scale=2 doubles pixels)

# Map styling that matches the ERoute palette (cream land, light-blue water,
# white roads, navy labels). Requires the "Maps Static API" to be enabled.
_MAP_STYLE = [
    "element:geometry|color:0xFFF8F2",
    "element:labels.icon|visibility:off",
    "element:labels.text.fill|color:0x0E425F",
    "element:labels.text.stroke|color:0xFFF8F2",
    "feature:administrative|element:geometry|visibility:off",
    "feature:poi|visibility:off",
    "feature:poi.medical|visibility:on",
    "feature:poi.medical|element:geometry|color:0xF3D6D6",
    "feature:poi.park|element:geometry|color:0xE3EEE4",
    "feature:road|element:geometry|color:0xFFFFFF",
    "feature:road|element:geometry.stroke|color:0xEBEBEB",
    "feature:road.highway|element:geometry|color:0xE6EEF5",
    "feature:transit|visibility:off",
    "feature:water|element:geometry|color:0xA3C1DA",
    "feature:water|element:labels.text.fill|color:0x0E425F",
]

LatLng = tuple[float, float]


def _get_gmaps_client() -> googlemaps.Client:
    """Create and return a Google Maps client."""
    if not GOOGLE_MAPS_API_KEY:
        raise ValueError(
            "GOOGLE_MAPS_API_KEY is not set. "
            "Please add it to your .env file."
        )
    return googlemaps.Client(key=GOOGLE_MAPS_API_KEY)


def geocode_location(location_text: str) -> LatLng:
    """
    Convert a textual location (e.g. 'Andheri West, Mumbai') into
    (latitude, longitude). Only used as a fallback when the device location
    is unavailable but the user mentioned where they are.
    Requires the Geocoding API.
    """
    client = _get_gmaps_client()
    results = client.geocode(location_text)
    if not results:
        raise ValueError(f"Could not geocode location: '{location_text}'")
    loc = results[0]["geometry"]["location"]
    return loc["lat"], loc["lng"]


def _format_duration(seconds: float) -> str:
    """Format a Routes API duration as a compact human-readable string."""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} mins"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours} hr {remaining_minutes} mins"


def _get_route(origin: LatLng, hospital: dict[str, Any]) -> dict[str, Any]:
    """Get one driving route using Google's Routes API."""
    response = requests.post(
        ROUTES_API_URL,
        params={"key": GOOGLE_MAPS_API_KEY},
        headers={
            "Content-Type": "application/json",
            "X-Goog-FieldMask": (
                "routes.duration,routes.distanceMeters,"
                "routes.polyline.encodedPolyline"
            ),
        },
        json={
            "origin": {"location": {"latLng": {
                "latitude": origin[0], "longitude": origin[1],
            }}},
            "destination": {"location": {"latLng": {
                "latitude": hospital["latitude"],
                "longitude": hospital["longitude"],
            }}},
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
            "polylineQuality": "OVERVIEW",
        },
        timeout=20,
    )
    if not response.ok:
        try:
            details = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            details = response.text
        raise RuntimeError(f"Google Routes API request failed: {details}")

    routes = response.json().get("routes", [])
    if not routes:
        raise RuntimeError("Google Routes API returned no route.")

    route = routes[0]
    duration = route.get("duration", "")
    if not isinstance(duration, str) or not duration.endswith("s"):
        raise RuntimeError("Google Routes API returned an invalid duration.")

    meters = route.get("distanceMeters", 0)
    seconds = float(duration[:-1])
    return {
        "distance": f"{meters / 1000:.1f} km",
        "distance_meters": meters,
        "duration": _format_duration(seconds),
        "duration_seconds": seconds,
        "polyline": route.get("polyline", {}).get("encodedPolyline", ""),
    }


def rank_hospitals_by_distance(
    origin: LatLng,
    hospitals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Rank hospitals by driving time from ``origin`` (shortest first).

    Each returned hospital dict is augmented with 'distance', 'duration',
    'duration_seconds', 'distance_meters' and 'polyline'. A hospital whose
    route cannot be computed is skipped; if none can be computed, raises.
    """
    if not hospitals:
        return []

    def route_one(hospital: dict[str, Any]) -> dict[str, Any] | None:
        if not (hospital.get("latitude") or hospital.get("longitude")):
            logger.warning("Skipping %s: no coordinates in the sheet", hospital.get("name"))
            return None
        try:
            return {**hospital, **_get_route(origin, hospital)}
        except Exception:
            logger.exception("Routing failed for %s", hospital.get("name"))
            return None

    with ThreadPoolExecutor(max_workers=min(8, len(hospitals))) as pool:
        results = list(pool.map(route_one, hospitals))

    ranked = [r for r in results if r]
    if not ranked:
        raise RuntimeError("Could not calculate a route to any candidate hospital.")
    ranked.sort(key=lambda h: h["duration_seconds"])
    return ranked


def _fit_size(width: int, height: int) -> tuple[int, int]:
    """Scale (width, height) down to the Static Maps limit, keeping the ratio."""
    ratio = min(MAX_STATIC_SIDE / max(width, 1), MAX_STATIC_SIDE / max(height, 1), 1.0)
    return max(100, int(width * ratio)), max(100, int(height * ratio))


def get_route_map(
    origin: LatLng,
    hospital: dict[str, Any],
    width: int = 640,
    height: int = 640,
) -> bytes | None:
    """
    Render a styled map (PNG bytes) showing the driving route from ``origin``
    (marker A) to ``hospital`` (marker B). The viewport is fitted automatically
    to the route. Returns None if the map cannot be produced.
    """
    if not GOOGLE_MAPS_API_KEY:
        return None

    w, h = _fit_size(width, height)
    base: list[tuple[str, str]] = [
        ("size", f"{w}x{h}"),
        ("scale", "2"),
        ("format", "png"),
        ("maptype", "roadmap"),
        ("markers", f"color:0x0E425F|label:A|{origin[0]},{origin[1]}"),
        ("markers", f"color:0xCC0000|label:B|{hospital['latitude']},{hospital['longitude']}"),
    ]
    base.extend(("style", rule) for rule in _MAP_STYLE)

    polyline = hospital.get("polyline")
    attempts = []
    if polyline:
        attempts.append(base + [("path", f"color:0x0E425FCC|weight:5|enc:{polyline}")])
    attempts.append(base)  # fallback: markers only

    for params in attempts:
        response = requests.get(
            STATIC_MAP_URL, params=params + [("key", GOOGLE_MAPS_API_KEY)], timeout=20
        )
        if response.ok and response.headers.get("Content-Type", "").startswith("image"):
            return response.content
        logger.warning(
            "Static map request failed (%s): %s",
            response.status_code, response.text[:200],
        )
    return None


def directions_url(origin: LatLng, hospital: dict[str, Any]) -> str:
    """Google Maps link for turn-by-turn navigation to the hospital."""
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={origin[0]},{origin[1]}"
        f"&destination={hospital['latitude']},{hospital['longitude']}"
        "&travelmode=driving"
    )
