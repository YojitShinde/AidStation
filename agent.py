"""
ERoute - ER Assistant
LangGraph agent: orchestrates Gemini for medical analysis, Google Sheets for
hospital resource matching, and Google Maps for routing from the device's
current location.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, END

from config import GEMINI_API_KEY, GEMINI_MODEL, ALL_RESOURCES
from location_service import DeviceLocation
from sheets_service import fetch_all_hospitals, find_hospitals_with_resources
from maps_service import geocode_location, rank_hospitals_by_distance

logger = logging.getLogger(__name__)

NO_LOCATION_MSG = (
    "Your location could not be determined. Turn on Location Services for this "
    "device (Windows: Settings → Privacy & security → Location) and try again."
)


def _response_text(content: Any) -> str:
    """Extract text from Gemini's string or multimodal content format."""
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
        if text_parts:
            return "".join(text_parts).strip()

    raise ValueError("Gemini returned a response without readable text.")


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------
class AgentState(TypedDict):
    """State that flows through the LangGraph nodes."""
    user_input: str                           # Raw user prompt
    origin: DeviceLocation | None             # Device location (GPS etc.)
    medical_concern: str                      # Extracted medical concern
    user_location: str                        # Location mentioned in text (fallback only)
    required_resources: list[str]             # Resources needed (determined by LLM)
    all_hospitals: list[dict[str, Any]]       # All hospitals fetched from Sheets
    matching_hospitals: list[dict[str, Any]]  # Hospitals with required resources
    ranked_hospitals: list[dict[str, Any]]    # Hospitals sorted by travel time
    routing_error: str                        # Location/Maps error, if any


# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------
def _get_llm() -> ChatGoogleGenerativeAI:
    if not GEMINI_API_KEY:
        raise ValueError(
            "GEMINI_API_KEY is not set. Please add it to your .env file."
        )
    return ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        google_api_key=GEMINI_API_KEY,
        temperature=0.1,
    )


# ---------------------------------------------------------------------------
# Node 1: Analyze medical concern & determine required resources
# ---------------------------------------------------------------------------
def analyze_medical_concern(state: AgentState) -> dict:
    """
    Use Gemini to:
    1. Extract the medical concern.
    2. Pick up a location only if the user happened to mention one.
    3. Determine which hospital resources are required.
    """
    llm = _get_llm()

    system_prompt = f"""You are a medical triage assistant. Your job is to analyze
a patient's described symptoms and determine which hospital resources they
would need.

Available hospital resources:
{json.dumps(ALL_RESOURCES, indent=2)}

Respond ONLY with a valid JSON object (no markdown, no explanation) with
exactly these keys:
- "medical_concern": a short summary of the patient's medical issue
- "user_location": the location the patient mentioned, or "" if none was mentioned
- "required_resources": a list of resource names (from the available list above)
  that the patient would likely need

Important guidelines for resource selection:
- For chest pain / cardiac issues → include "Emergency Department", "ICU", "Cardiology", "Oxygen Supply"
- For breathing difficulties → include "Emergency Department", "ICU", "Ventilator", "Oxygen Supply"
- For trauma / accidents → include "Emergency Department", "Emergency Surgery", "X-Ray", "Blood Bank", "ICU"
- For neurological issues → include "Emergency Department", "Neurology", "CT Scanner", "MRI Scanner"
- For kidney issues → include "Emergency Department", "Dialysis"
- For pregnancy emergencies → include "Emergency Department", "Obstetrics", "ICU"
- For pediatric emergencies → include "Emergency Department", "Pediatric Emergency"
- Always include "Emergency Department" for any emergency.
- If a scan might be needed, include "CT Scanner" or "MRI Scanner" or "X-Ray" as appropriate.
"""

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=state["user_input"]),
    ]

    response = llm.invoke(messages)
    content = _response_text(response.content)

    # Strip markdown code fences if present
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()
    if content.startswith("json"):
        content = content[4:].strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Gemini returned an invalid triage response. Please try again."
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError("Gemini returned an invalid triage response.")

    requested_resources = parsed.get("required_resources", [])
    if not isinstance(requested_resources, list):
        raise ValueError("Gemini returned invalid hospital resource data.")
    required_resources = [
        resource for resource in requested_resources
        if isinstance(resource, str) and resource in ALL_RESOURCES
    ]
    if "Emergency Department" not in required_resources:
        required_resources.insert(0, "Emergency Department")

    return {
        "medical_concern": parsed.get("medical_concern") or state["user_input"],
        "user_location": str(parsed.get("user_location") or "").strip(),
        "required_resources": required_resources,
    }


# ---------------------------------------------------------------------------
# Node 2: Fetch hospital data from Google Sheets
# ---------------------------------------------------------------------------
def fetch_hospital_data(_state: AgentState) -> dict:
    """Fetch all hospital data from the Google Sheets spreadsheet."""
    return {"all_hospitals": fetch_all_hospitals()}


# ---------------------------------------------------------------------------
# Node 3: Filter hospitals by required resources
# ---------------------------------------------------------------------------
def filter_hospitals(state: AgentState) -> dict:
    """Keep only hospitals with all required resources available."""
    matching = find_hospitals_with_resources(
        state["all_hospitals"],
        state["required_resources"],
    )
    return {"matching_hospitals": matching}


# ---------------------------------------------------------------------------
# Node 4: Rank hospitals by travel time from the device location
# ---------------------------------------------------------------------------
def rank_by_distance(state: AgentState) -> dict:
    """Rank the matching hospitals by driving time from the user's location."""
    if not state["matching_hospitals"]:
        return {"ranked_hospitals": [], "routing_error": ""}

    origin = state["origin"]

    # Fallback: device location unavailable, but the user mentioned a place.
    if origin is None and state["user_location"]:
        try:
            lat, lng = geocode_location(state["user_location"])
            origin = DeviceLocation(lat, lng, None, "mentioned")
        except Exception:
            logger.exception("Could not geocode the location mentioned by the user")

    if origin is None:
        return {"ranked_hospitals": [], "routing_error": NO_LOCATION_MSG}

    try:
        ranked = rank_hospitals_by_distance(
            (origin.latitude, origin.longitude), state["matching_hospitals"]
        )
    except Exception:
        logger.exception("Google Maps ranking failed")
        return {
            "origin": origin,
            "ranked_hospitals": [],
            "routing_error": (
                "Google Maps could not calculate travel times. "
                "Please check your connection and the Maps API configuration."
            ),
        }
    return {"origin": origin, "ranked_hospitals": ranked, "routing_error": ""}


# ---------------------------------------------------------------------------
# Build the LangGraph
# ---------------------------------------------------------------------------
def build_graph() -> StateGraph:
    """Construct and compile the ERoute agent graph."""
    graph = StateGraph(AgentState)

    graph.add_node("analyze", analyze_medical_concern)
    graph.add_node("fetch_data", fetch_hospital_data)
    graph.add_node("filter", filter_hospitals)
    graph.add_node("rank", rank_by_distance)

    graph.set_entry_point("analyze")
    graph.add_edge("analyze", "fetch_data")
    graph.add_edge("fetch_data", "filter")
    graph.add_edge("filter", "rank")
    graph.add_edge("rank", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def run_agent_detailed(
    user_input: str,
    origin: DeviceLocation | None = None,
) -> dict[str, Any]:
    """
    Run the full ERoute pipeline for one query.

    Returns a dict with:
      concern             – short summary of the medical concern
      required_resources  – resources the hospital must have
      hospitals           – matching hospitals, nearest first. Each has name,
                            address, phone, distance, duration, polyline, lat/lng
      origin              – the DeviceLocation actually used (or None)
      error               – user-facing error text ("" if none)
      no_match            – True if no hospital has every required resource
    """
    app = build_graph()
    initial_state: AgentState = {
        "user_input": user_input,
        "origin": origin,
        "medical_concern": "",
        "user_location": "",
        "required_resources": [],
        "all_hospitals": [],
        "matching_hospitals": [],
        "ranked_hospitals": [],
        "routing_error": "",
    }
    result = app.invoke(initial_state)

    return {
        "concern": result["medical_concern"],
        "required_resources": result["required_resources"],
        "hospitals": result["ranked_hospitals"],
        "origin": result.get("origin"),
        "error": result["routing_error"],
        "no_match": not result["matching_hospitals"],
    }
