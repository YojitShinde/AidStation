"""Local WMS retrieval and grounded responses for the AidPilot module."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pypdf import PdfReader

from config import GEMINI_API_KEY, GEMINI_MODEL

DOCUMENTS_DIR = Path(__file__).parent / "documents"


def default_sources() -> list[Path]:
    return sorted(DOCUMENTS_DIR.glob("*.pdf"), key=lambda path: path.name.lower())


def read_source(path: Path) -> str:
    """Extract text from a source PDF for viewing and retrieval."""
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


def _chunks(text: str, size: int = 1800, overlap: int = 250) -> list[str]:
    words = text.split()
    chunks: list[str] = []
    start = 0
    while start < len(words):
        chunk = " ".join(words[start:start + size])
        if chunk:
            chunks.append(chunk)
        start += max(size - overlap, 1)
    return chunks


def _terms(text: str) -> set[str]:
    return {term for term in re.findall(r"[a-z0-9]{3,}", text.lower())}


def _response_text(content: Any) -> str:
    """Extract text from Gemini's string or multimodal content format."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    return ""


def retrieve(question: str, sources: list[Path], limit: int = 6) -> list[dict[str, str]]:
    """Return the most keyword-relevant passages from the selected sources."""
    question_terms = _terms(question)
    matches: list[tuple[int, str, str]] = []
    for source in sources:
        text = read_source(source)
        for chunk in _chunks(text):
            score = len(question_terms & _terms(chunk))
            if score:
                matches.append((score, source.name, chunk))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [{"source": source, "text": text} for _, source, text in matches[:limit]]


def ask(question: str, sources: list[Path]) -> str:
    if not sources:
        raise ValueError("Add at least one WMS source before asking AidPilot.")
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set. Please add it to your .env file.")

    passages = retrieve(question, sources)
    if not passages:
        raise ValueError(
            "No relevant passage was found in the selected sources. "
            "Try describing the circumstance with more detail."
        )
    context = "\n\n".join(
        f"[Source: {item['source']}]\n{item['text']}" for item in passages
    )
    llm = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL, google_api_key=GEMINI_API_KEY, temperature=0.1
    )
    response = llm.invoke([
        SystemMessage(content=(
            "You are AidPilot, a paramedic field assistant. Answer only from the "
            "provided WMS excerpts. Give clear, practical procedures and precautions "
            "for the described circumstance. Do not diagnose, invent missing steps, "
            "or present yourself as a substitute for emergency services. State when "
            "to call emergency services. Cite the source filename after each relevant "
            "section using [Source: filename]. If the excerpts do not support an "
            "answer, say so plainly."
        )),
        HumanMessage(content=f"Medical circumstance:\n{question}\n\nWMS excerpts:\n{context}"),
    ])
    content = _response_text(response.content)
    if content:
        return content
    metadata = getattr(response, "response_metadata", {})
    finish_reason = metadata.get("finish_reason") if isinstance(metadata, dict) else None
    if finish_reason:
        raise ValueError(
            f"AidPilot returned no text (generation stopped: {finish_reason}). "
            "Try rephrasing the request."
        )
    raise ValueError(
        "AidPilot returned no readable text. Please try again or rephrase the request."
    )
