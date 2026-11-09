"""
ERoute - ER Assistant
LangGraph agent: orchestrates Gemini for medical analysis, sheets for
hospital resource matching, and Google Maps for routing.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, END

from config import GEMINI_API_KEY, GEMINI_MODEL, ALL_RESOURCES
from sheets_service import fetch_all_hospitals, find_hospitals_with_resources
from maps_service import rank_hospitals_by_distance

logger = logging.getLogger(__name__)


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
    user_input: str                       # Raw user prompt
    medical_concern: str                  # Extracted medical concern
    user_location: str                    # Extracted user location
    required_resources: list[str]         # Resources needed (determined by LLM)
    all_hospitals: list[dict[str, Any]]   # All hospitals fetched from Sheets
    matching_hospitals: list[dict[str, Any]]  # Hospitals with required resources
    ranked_hospitals: list[dict[str, Any]]    # Hospitals sorted by distance
    routing_error: str                         # Maps error, if routing failed
    final_response: str                   # Final formatted answer


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
    2. Extract the user's location.
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
- "user_location": the location the patient mentioned
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
        "medical_concern": parsed.get("medical_concern", state["user_input"]),
        "user_location": str(parsed.get("user_location", "")).strip(),
        "required_resources": required_resources,
    }


# ---------------------------------------------------------------------------
# Node 2: Fetch hospital data from Google Sheets
# ---------------------------------------------------------------------------
def fetch_hospital_data(_state: AgentState) -> dict:
    """Fetch all hospital data from the Google Sheets spreadsheet."""
    hospitals = fetch_all_hospitals()
    return {"all_hospitals": hospitals}


# ---------------------------------------------------------------------------
# Node 3: Filter hospitals by required resources
# ---------------------------------------------------------------------------
def filter_hospitals(state: AgentState) -> dict:
    """Filter hospitals to only those with all required resources available."""
    matching = find_hospitals_with_resources(
        state["all_hospitals"],
        state["required_resources"],
    )
    return {"matching_hospitals": matching}


# ---------------------------------------------------------------------------
# Node 4: Rank hospitals by distance via Google Maps
# ---------------------------------------------------------------------------
def rank_by_distance(state: AgentState) -> dict:
    """Rank the matching hospitals by travel time from the user's location."""
    if not state["matching_hospitals"]:
        return {"ranked_hospitals": []}

    user_loc = state["user_location"]
    if not user_loc:
        return {
            "ranked_hospitals": [],
            "routing_error": (
                "A location is required to calculate the nearest hospital. "
                "Please include your current area or address."
            ),
        }

    try:
        ranked = rank_hospitals_by_distance(user_loc, state["matching_hospitals"])
    except Exception:
        logger.exception("Google Maps ranking failed")
        return {
            "ranked_hospitals": [],
            "routing_error": (
                "Google Maps could not calculate travel times. "
                "Please verify the location and Maps API configuration."
            ),
        }

    if not ranked:
        return {
            "ranked_hospitals": [],
            "routing_error": "No candidate hospital could be reached from the provided location.",
        }
    return {"ranked_hospitals": ranked, "routing_error": ""}


# ---------------------------------------------------------------------------
# Node 5: Generate the final response with Gemini
# ---------------------------------------------------------------------------
def generate_response(state: AgentState) -> dict:
    """Use Gemini to compose a helpful, human-readable response."""
    if state["routing_error"]:
        return {
            "final_response": (
                f"⚠️ {state['routing_error']}\n\n"
                f"**Medical Concern:** {state['medical_concern']}\n"
                f"**Required Resources:** {', '.join(state['required_resources'])}\n\n"
                "Please call emergency services (112) immediately if this is life-threatening."
            )
        }

    if not state["ranked_hospitals"]:
        # No hospitals matched — inform the user
        no_match_msg = (
            "⚠️ Sorry, none of the hospitals in our database currently have "
            "all the required resources available for your medical concern.\n\n"
            f"**Medical Concern:** {state['medical_concern']}\n"
            f"**Required Resources:** {', '.join(state['required_resources'])}\n\n"
            "Please call emergency services (112) immediately."
        )
        return {"final_response": no_match_msg}

    llm = _get_llm()

    # Build context for Gemini
    hospital_summaries = []
    for i, h in enumerate(state["ranked_hospitals"], 1):
        entry = (
            f"Hospital {i}:\n"
            f"  Name: {h['name']}\n"
            f"  Address: {h['address']}\n"
            f"  Phone: {h['phone']}\n"
        )
        if "distance" in h:
            entry += f"  Distance: {h['distance']}\n"
        if "duration" in h:
            entry += f"  Travel Time: {h['duration']}\n"

        # Available resources
        avail = [
            r for r, info in h.get("resources", {}).items()
            if info.get("available")
        ]
        entry += f"  Available Resources: {', '.join(avail)}\n"
        hospital_summaries.append(entry)

    context = "\n".join(hospital_summaries)

    system_prompt = """You are ERoute, an emergency room assistant helping
patients find the nearest hospital. Given the patient's medical concern,
required resources, and ranked hospital information, compose a clear,
empathetic, and actionable response.

Format your response with:
1. A brief acknowledgement of the patient's concern.
2. Your recommendation (the nearest/quickest hospital).
3. Hospital details (Name, Address, Phone, Distance, Travel Time).
4. If there are alternative hospitals, list them briefly.
5. A reminder to call emergency services (112) if the situation is critical.

Keep the response concise and easy to read. Use emojis sparingly for visual cues."""

    user_msg = f"""Patient's Concern: {state['medical_concern']}
Location: {state['user_location']}
Required Resources: {', '.join(state['required_resources'])}

Ranked Hospitals (nearest first):
{context}"""

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ]

    response = llm.invoke(messages)
    return {"final_response": _response_text(response.content)}


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
    graph.add_node("respond", generate_response)

    graph.set_entry_point("analyze")
    graph.add_edge("analyze", "fetch_data")
    graph.add_edge("fetch_data", "filter")
    graph.add_edge("filter", "rank")
    graph.add_edge("rank", "respond")
    graph.add_edge("respond", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def run_agent(user_input: str) -> str:
    """
    Run the full ERoute pipeline on a single user query.

    Returns the final response string.
    """
    app = build_graph()
    initial_state: AgentState = {
        "user_input": user_input,
        "medical_concern": "",
        "user_location": "",
        "required_resources": [],
        "all_hospitals": [],
        "matching_hospitals": [],
        "ranked_hospitals": [],
        "routing_error": "",
        "final_response": "",
    }
    result = app.invoke(initial_state)
    return result["final_response"]
