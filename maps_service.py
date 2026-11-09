"""
ERoute - ER Assistant
Google Maps service: computes travel distances/durations between the user's
location and candidate hospitals.
"""

from typing import Any

import googlemaps
import requests

from config import GOOGLE_MAPS_API_KEY

ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


def _get_gmaps_client() -> googlemaps.Client:
    """Create and return a Google Maps client."""
    if not GOOGLE_MAPS_API_KEY:
        raise ValueError(
            "GOOGLE_MAPS_API_KEY is not set. "
            "Please add it to your .env file."
        )
    return googlemaps.Client(key=GOOGLE_MAPS_API_KEY)


def geocode_location(location_text: str) -> tuple[float, float]:
    """
    Convert a textual location (e.g. 'Andheri West, Mumbai') into
    (latitude, longitude) using the Google Maps Geocoding API.
    """
    client = _get_gmaps_client()
    results = client.geocode(location_text)
    if not results:
        raise ValueError(f"Could not geocode location: '{location_text}'")
    loc = results[0]["geometry"]["location"]
    return loc["lat"], loc["lng"]


def get_nearest_hospital(
    user_location: str,
    hospitals: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """
    Use the Google Maps Distance Matrix API to find the hospital that is
    nearest (by travel time) to the user's location.

    Parameters
    ----------
    user_location : str
        Textual description or 'lat,lng' of the user's current location.
    hospitals : list[dict]
        List of hospital dicts, each with 'latitude' and 'longitude'.

    Returns
    -------
    The hospital dict augmented with 'distance' and 'duration' keys,
    or None if no hospital could be reached.
    """
    if not hospitals:
        return None

    client = _get_gmaps_client()

    destinations = [
        f"{h['latitude']},{h['longitude']}" for h in hospitals
    ]

    result = client.distance_matrix(
        origins=[user_location],
        destinations=destinations,
        mode="driving",
    )

    best_hospital = None
    best_duration_seconds = float("inf")

    elements = result.get("rows", [{}])[0].get("elements", [])
    for idx, element in enumerate(elements):
        if element.get("status") != "OK":
            continue
        duration_sec = element["duration"]["value"]
        if duration_sec < best_duration_seconds:
            best_duration_seconds = duration_sec
            best_hospital = hospitals[idx].copy()
            best_hospital["distance"] = element["distance"]["text"]
            best_hospital["duration"] = element["duration"]["text"]

    return best_hospital


def _get_route(origin: str, hospital: dict[str, Any]) -> dict[str, Any]:
    """Get one driving route using Google's current Routes API."""
    response = requests.post(
        ROUTES_API_URL,
        params={"key": GOOGLE_MAPS_API_KEY},
        headers={
            "Content-Type": "application/json",
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
        },
        json={
            "origin": {"address": origin},
            "destination": {
                "location": {
                    "latLng": {
                        "latitude": hospital["latitude"],
                        "longitude": hospital["longitude"],
                    }
                }
            },
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
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

    return {
        "distance": f"{route.get('distanceMeters', 0) / 1000:.1f} km",
        "duration": _format_duration(float(duration[:-1])),
        "duration_seconds": float(duration[:-1]),
    }


def _format_duration(seconds: float) -> str:
    """Format a Routes API duration as a compact human-readable string."""
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} mins"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours} hr {remaining_minutes} mins"


def rank_hospitals_by_distance(
    user_location: str,
    hospitals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Rank hospitals by travel time from the user's location (shortest first).

    Returns a new list of hospital dicts, each augmented with
    'distance' and 'duration' keys.
    """
    if not hospitals:
        return []

    ranked = []
    for hospital in hospitals:
        route = _get_route(user_location, hospital)
        ranked.append({**hospital, **route})

    ranked.sort(key=lambda h: h.get("duration_seconds", float("inf")))
    return ranked
