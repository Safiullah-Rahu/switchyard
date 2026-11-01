"""Structured outputs. Every specialist returns one of these, never free text."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class QualifiedLead(BaseModel):
    contact_name: str
    email: str
    phone: str | None
    company: str | None
    segment: Literal["residential", "commercial", "agricultural"]
    region: str = Field(description="One of: uk-south, uk-north, de, us-ca")
    site_address: str | None
    annual_consumption_kwh: float | None
    roof_area_m2: float | None
    interested_in: list[Literal["solar", "battery", "ev_charging", "heat_pump"]]
    timeline: str | None
    survey_preference: str | None
    existing_contact_id: str | None
    qualification: Literal["hot", "warm", "cold"]
    rationale: str
    missing_information: list[str]


class ServiceCase(BaseModel):
    customer_email: str
    customer_name: str | None
    category: Literal["fault", "billing", "maintenance", "other"]
    urgency: Literal["low", "normal", "high"]
    summary: str
    reply_subject: str
    reply_body: str


class CitedFigure(BaseModel):
    key: str = Field(description="A key from get_estimate().figures, e.g. payback_years")
    value: float


class ProposalDraft(BaseModel):
    title: str
    executive_summary: str
    body_markdown: str
    email_subject: str
    email_body: str
    figures: list[CitedFigure]


class ReviewIssue(BaseModel):
    severity: Literal["blocker", "minor"]
    description: str


class ReviewVerdict(BaseModel):
    approve: bool
    issues: list[ReviewIssue]
    summary: str


class SurveySlot(BaseModel):
    start: str = Field(description="ISO start time copied exactly from calendar_free_slots")
    end: str = Field(description="ISO end time copied exactly from calendar_free_slots")
    reason: str
