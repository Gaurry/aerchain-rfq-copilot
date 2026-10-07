"""What the AI must return when it reads a vendor response.

Design rule: extract LITERALLY. Values, units and currencies exactly as the
vendor wrote them, with the source text as evidence. No conversion happens
here; normalization is deterministic code (core/normalize.py)."""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    quote: str = Field(description="Exact text (or a faithful transcription for images) the value came from")
    location: Optional[str] = Field(None, description="Where: page, sheet/row, paragraph, image region")


class PriceUnit(str, Enum):
    per_piece = "per_piece"
    per_100 = "per_100"
    per_1000 = "per_1000"
    per_kg = "per_kg"
    same_as_last_year = "same_as_last_year"
    unknown = "unknown"


class QuotedItem(BaseModel):
    vendor_ref: Optional[str] = Field(None, description="Code as written by the vendor (theirs or ours)")
    description: str = Field(description="Item description as written")
    product_type: Literal["box", "tray", "die_cut", "partition", "pad", "unknown"] = Field(
        "unknown", description="What kind of item this is, from the vendor's wording")
    size_as_written: Optional[str] = Field(None, description="Dimensions exactly as written, e.g. '16 x 12 x 8'")
    size_unit: Literal["mm", "inch", "unknown"] = "unknown"
    ply: Optional[int] = None
    paper_spec: Optional[str] = Field(None, description="GSM / BF as written; null if not stated")
    print_colours: Optional[int] = None
    price_text: str = Field(description="Price exactly as written, including symbols and markers like '*'")
    price_value: Optional[float] = Field(None, description="Numeric value as written, no conversion")
    currency: Literal["INR", "USD", "other", "unknown"] = "unknown"
    price_unit: PriceUnit = PriceUnit.unknown
    scope: Literal["single_item", "category", "all_remaining"] = Field(
        "single_item", description="category = one rate for a group (e.g. 'the 5-ply'); all_remaining = 'rest'")
    scope_description: Optional[str] = Field(None, description="For category/all_remaining: the vendor's words")
    is_alternate_spec: bool = Field(False, description="Vendor offers a different spec than requested")
    alternate_note: Optional[str] = None
    plain_box_rate: bool = Field(False, description="Vendor says the rate excludes printing")
    handwritten: bool = Field(False, description="Value is a handwritten correction or note")
    legibility: Literal["clear", "unclear"] = "clear"
    evidence: Evidence
    notes: Optional[str] = None


class Discount(BaseModel):
    description: str
    percent: float = Field(description="e.g. 3 for 3%")
    condition: Optional[str] = Field(None, description="Condition exactly as stated, null if unconditional")
    evidence: Evidence


class Adder(BaseModel):
    description: str
    amount: float
    unit: Literal["per_piece_per_colour", "per_100_per_colour", "per_piece", "percent"]
    evidence: Evidence


class QAnswer(BaseModel):
    question_no: int
    answer_text: str = Field(description="The vendor's answer as written")
    status: Literal["yes", "no", "partial", "vague", "not_answered", "refers_elsewhere"]
    evidence: Optional[Evidence] = None


class Certificate(BaseModel):
    standard: str
    certificate_no: Optional[str] = None
    valid_until: Optional[str] = Field(None, description="ISO date YYYY-MM-DD as printed")
    source: Literal["attached_document", "stated_in_response"]
    evidence: Evidence


class VendorResponse(BaseModel):
    vendor_name: str
    source_files: list[str]
    quote_date: Optional[str] = None
    gst_treatment: Literal["inclusive", "exclusive", "not_stated"] = "not_stated"
    gst_rate_percent: Optional[float] = None
    freight_treatment: Literal["delivered", "ex_works", "extra", "not_stated"] = "not_stated"
    payment_days: Optional[int] = None
    validity_days: Optional[int] = None
    lead_time_text: Optional[str] = None
    discounts: list[Discount] = []
    adders: list[Adder] = []
    items: list[QuotedItem] = []
    questionnaire: list[QAnswer] = []
    certificates: list[Certificate] = []
    refers_to_prior: list[str] = Field([], description="Phrases pointing to earlier documents, e.g. 'same as last year'")
    terms_evidence: list[Evidence] = []
    extraction_warnings: list[str] = Field([], description="Anything the model was unsure about")


class Match(BaseModel):
    rfq_code: str
    item_index: int
    method: Literal["code", "spec", "description", "category_rule", "remaining_rule"]
    size_deviation: Optional[float] = None
    confidence: float
    reason: str


class AIMatch(BaseModel):
    item_index: int
    rfq_code: str = Field(description="The RFQ line code this item matches, e.g. 'CB-304', or exactly 'NONE' if no line fits")
    confidence: Literal["high", "medium", "low"]
    reason: str


class AIMatchResult(BaseModel):
    matches: list[AIMatch]
