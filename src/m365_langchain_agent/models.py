"""Pydantic request/response schemas for API validation."""

from pydantic import BaseModel, Field


class TestQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000)
    conversation_id: str = "test-session"
    model: str | None = None
    top_k: int | None = Field(None, ge=1, le=50)
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    filter: str | None = None


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str | None = None


class StarterPrompt(BaseModel):
    label: str
    message: str
