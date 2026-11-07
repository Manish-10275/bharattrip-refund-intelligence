from enum import Enum
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field, ConfigDict, field_validator


class ComplaintCategory(str, Enum):
    """Defined complaint categories for refund intake messages."""
    REFUND_STATUS = "refund_status"
    NO_UPDATE = "no_update"
    MISSING_REFERENCE = "missing_reference"
    AMOUNT_DISPUTE = "amount_dispute"
    ELIGIBILITY_DISPUTE = "eligibility_dispute"
    OTHER = "other"


class RefundIntakeExtraction(BaseModel):
    """
    Validated extraction schema for customer refund messages.
    Conforms to strict Groq structured outputs.
    Unknown or absent values must strictly be null, never invented.
    """
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        use_enum_values=True
    )

    customer_name: Optional[str] = Field(
        default=None,
        description="Full name of customer/passenger if explicitly stated, otherwise null."
    )
    agency_name: Optional[str] = Field(
        default=None,
        description="Travel agency or OTA name (e.g. MakeMyTrip, EaseMyTrip, GoFly) if explicitly stated, otherwise null."
    )
    booking_reference: Optional[str] = Field(
        default=None,
        description="Airline PNR or booking reference code if explicitly stated, otherwise null."
    )
    refund_reference: Optional[str] = Field(
        default=None,
        description="Specific refund claim or ticket reference (e.g. RF-XXXX) if explicitly stated, otherwise null."
    )
    route: Optional[str] = Field(
        default=None,
        description="Flight travel route (e.g. DEL-BOM, Delhi to Mumbai) if explicitly stated, otherwise null."
    )
    refund_amount: Optional[float] = Field(
        default=None,
        description="Claimed or disputed refund monetary amount as a numeric value, otherwise null."
    )
    currency: Optional[str] = Field(
        default=None,
        description="Explicit currency code or symbol (e.g. INR, ₹, USD) if explicitly stated in message, otherwise null. Must not be assumed or defaulted based on company location, agency, or route."
    )
    request_date: Optional[str] = Field(
        default=None,
        description="Date of cancellation, flight, or request in ISO format (YYYY-MM-DD) or original text if stated, otherwise null."
    )
    complaint_category: ComplaintCategory = Field(
        ...,
        description="Primary complaint category strictly chosen from defined enum values."
    )
    missing_information: List[str] = Field(
        default_factory=list,
        description="Explicit list of missing critical data fields needed to process refund (e.g. ['missing_booking_reference', 'missing_amount'])."
    )
    summary: str = Field(
        ...,
        min_length=1,
        description="Concise factual summary of the customer's communication. Do not include assumptions or claims of refund approval."
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Extraction confidence score between 0.0 and 1.0 reflecting completeness and certainty of extracted entities."
    )
    human_review_required: bool = Field(
        ...,
        description="True if message has ambiguities, missing references, dispute claims, or requires operations team verification."
    )
    human_review_reason: Optional[str] = Field(
        default=None,
        description="Specific justification why human review is required when human_review_required is true, or null when review is not required."
    )

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if v < 0.0 or v > 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return round(v, 2)


def get_groq_strict_json_schema() -> Dict[str, Any]:
    """
    Constructs a JSON schema compatible with Groq/OpenAI strict structured outputs.
    All properties are required in the schema with nullable types for optional fields.
    """
    return {
        "name": "refund_intake_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "customer_name": {
                    "type": ["string", "null"],
                    "description": "Full name of customer/passenger if explicitly stated, otherwise null."
                },
                "agency_name": {
                    "type": ["string", "null"],
                    "description": "Travel agency or OTA name if explicitly stated, otherwise null."
                },
                "booking_reference": {
                    "type": ["string", "null"],
                    "description": "Airline PNR or booking reference code if explicitly stated, otherwise null."
                },
                "refund_reference": {
                    "type": ["string", "null"],
                    "description": "Specific refund claim reference (e.g. RF-XXXX) if explicitly stated, otherwise null."
                },
                "route": {
                    "type": ["string", "null"],
                    "description": "Flight route if explicitly stated, otherwise null."
                },
                "refund_amount": {
                    "type": ["number", "null"],
                    "description": "Numeric refund amount if stated, otherwise null."
                },
                "currency": {
                    "type": ["string", "null"],
                    "description": "Explicit currency code or symbol if stated in message, otherwise null. Never assume or default."
                },
                "request_date": {
                    "type": ["string", "null"],
                    "description": "Date if stated, otherwise null."
                },
                "complaint_category": {
                    "type": "string",
                    "enum": [c.value for c in ComplaintCategory],
                    "description": "Defined category."
                },
                "missing_information": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of missing critical fields."
                },
                "summary": {
                    "type": "string",
                    "description": "Concise factual summary."
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence score from 0.0 to 1.0."
                },
                "human_review_required": {
                    "type": "boolean",
                    "description": "Whether human review is required."
                },
                "human_review_reason": {
                    "type": ["string", "null"],
                    "description": "Specific justification why human review is required, or null if review is not required."
                }
            },
            "required": [
                "customer_name",
                "agency_name",
                "booking_reference",
                "refund_reference",
                "route",
                "refund_amount",
                "currency",
                "request_date",
                "complaint_category",
                "missing_information",
                "summary",
                "confidence",
                "human_review_required",
                "human_review_reason"
            ],
            "additionalProperties": False
        }
    }
