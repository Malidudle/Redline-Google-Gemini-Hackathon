from dataclasses import dataclass, field, asdict
from typing import Literal, Optional, Any
from enum import Enum
import time, uuid

class Exemption(str, Enum):
    """FOIA 2000 exemptions. Label text is what renders in the UI chip."""
    S40_2  = "s.40(2)"   # personal information (third party)
    S41    = "s.41"      # information provided in confidence
    S43_2  = "s.43(2)"   # commercial interests
    S36    = "s.36"      # prejudice to conduct of public affairs
    S38    = "s.38"      # health and safety
    S31    = "s.31"      # law enforcement
    S42    = "s.42"      # legal professional privilege
    S35    = "s.35"      # formulation of government policy

EXEMPTION_LABEL = {
    Exemption.S40_2: "personal data",
    Exemption.S41:   "provided in confidence",
    Exemption.S43_2: "commercial interests",
    Exemption.S36:   "conduct of public affairs",
    Exemption.S38:   "health and safety",
    Exemption.S31:   "law enforcement",
    Exemption.S42:   "legal privilege",
    Exemption.S35:   "policy formulation",
}

EXEMPTION_COLOUR = {           # frontend uses these exact values
    Exemption.S40_2: "#C2410C",
    Exemption.S41:   "#7C3AED",
    Exemption.S43_2: "#0F766E",
    Exemption.S36:   "#B45309",
    Exemption.S38:   "#BE123C",
    Exemption.S31:   "#1D4ED8",
    Exemption.S42:   "#4D7C0F",
    Exemption.S35:   "#9333EA",
}

# Statutory descriptions + the applied test, used by the redaction schedule (A4).
EXEMPTION_STATUTE = {
    Exemption.S40_2: ("Personal information", "absolute"),
    Exemption.S41:   ("Information provided in confidence", "absolute"),
    Exemption.S43_2: ("Commercial interests", "qualified"),
    Exemption.S36:   ("Prejudice to effective conduct of public affairs", "qualified"),
    Exemption.S38:   ("Health and safety", "qualified"),
    Exemption.S31:   ("Law enforcement", "qualified"),
    Exemption.S42:   ("Legal professional privilege", "qualified"),
    Exemption.S35:   ("Formulation of government policy", "qualified"),
}

@dataclass
class RedactionSpan:
    start: int                    # char offset into Segment.text
    end: int                      # exclusive
    exemption: Exemption
    surface: str                  # the exact original text (for the schedule)
    source: Literal["rule", "model"]
    confidence: float             # 0.0-1.0; rule matches are 1.0

@dataclass
class Segment:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    speaker: str = "Speaker"
    text: str = ""
    t_start: float = 0.0          # seconds from session start
    t_end: float = 0.0
    final: bool = False           # False = interim whisper hypothesis
    spans: list[RedactionSpan] = field(default_factory=list)
    redaction_state: Literal["pending", "done", "failed"] = "pending"

def to_wire(obj: Any) -> Any:
    """Dataclass -> JSON-safe dict. Exemption is a str Enum so it serialises as its value."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, list):
        return [to_wire(o) for o in obj]
    return obj

# ---- WebSocket envelope: every message is {"type": ..., "payload": ...} ----
# server -> client
#   "segment.partial"  payload: Segment          (final=False, spans=[])
#   "segment.final"    payload: Segment          (final=True,  spans=[] initially)
#   "segment.redacted" payload: {id, spans, redaction_state}
#   "session.stats"    payload: {bytes_egress: int, segments: int, redactions: int,
#                                latency_ms_p50: float}
#   "minutes.ready"    payload: {decisions: [...], actions: [...], attendees: [...]}
#   "export.ready"     payload: {path: str}
# client -> server
#   "session.start"    payload: {title: str, classification: str,
#                                source: "mic"|"replay"}   # replay = demo/seed_transcript.json
#   "session.stop"     payload: {}
#   "export.request"   payload: {format: "html"|"docx"}
#   "redaction.override" payload: {segment_id, span_index, action: "keep"|"remove"}
