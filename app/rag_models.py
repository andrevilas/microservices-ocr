"""Public RAG delivery transport models; no remote URLs or credentials."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

DeliveryId = Annotated[str, Field(strict=True, pattern="^[0-9a-f]{64}$")]
DestinationKey = Annotated[str, Field(strict=True, pattern="^[a-zA-Z0-9_-]{1,64}$")]


class DeliveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"job_id": "1" * 32, "destination": "normas"}]})
    job_id: str = Field(strict=True, pattern="^[0-9a-f]{32}$")
    destination: DestinationKey


DeliveryState = Literal["queued", "sending", "retry", "indexed", "resolution", "canceled"]


class DeliveryStatus(BaseModel):
    id: DeliveryId
    job_id: str
    state: DeliveryState
    deadline: float
    attempts: int
    next_attempt: float
    error: str | None


class DeliveryList(BaseModel):
    """Owned deliveries; a human administrator sees every owner."""
    items: list[DeliveryStatus]


class DeliveryOperations(BaseModel):
    """Aggregate worker state for administrators; no documents, URLs or tokens."""
    enabled: bool
    worker_running: bool
    worker_failure: str | None
    states: dict[str, int]
    due: int
    near_deadline: int
    requires_resolution: int
    queued_payload_bytes: int


class RecoveryLink(BaseModel):
    delivery_id: DeliveryId
    content_url: str
    size_bytes: int
    sha256: str


class DestinationView(BaseModel):
    key: DestinationKey
    destination_id: str
    source_kind: str
    process_reference: str | None


class DestinationList(BaseModel):
    items: list[DestinationView]
