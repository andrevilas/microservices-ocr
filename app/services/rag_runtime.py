"""Explicit opt-in runtime from a bounded operator configuration file."""
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.application import ApplicationError
from app.services.rag_application import RagApplication
from app.services.rag_delivery import Destination
from app.services.rag_delivery_contract import SafeId
from app.services.rag_outbox import DeliveryPolicy, RagOutbox
from app.services.rag_worker import RagDeliveryWorker


class BindingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(pattern="^[a-zA-Z0-9_-]{1,64}$")
    url: str
    # Same constraints as the ocr-rag/1 manifest, so a misconfigured binding
    # fails at startup instead of surfacing later as an OCR integrity error.
    destination_id: SafeId
    owner: str
    source_kind: Literal["process_record", "historical_reference", "normative_source"]
    process_reference: SafeId | None = None
    token: str = Field(repr=False)
    # Highest field sensitivity exported in ocr-graph/1 deliveries to this destination.
    graph_sensitivity_ceiling: Literal["public", "internal", "restricted"] = "internal"

    @model_validator(mode="after")
    def _process_binding(self):
        if self.source_kind == "process_record" and self.process_reference is None:
            raise ValueError("process_reference is required for process records")
        return self


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy: dict[str, int | None]
    destinations: list[BindingConfig] = Field(min_length=1, max_length=100)
    interval_seconds: int = Field(ge=1, le=86400)
    batch_size: int = Field(ge=1, le=100)
    warning_seconds: int = Field(ge=0, le=2147483647)
    shutdown_seconds: int = Field(ge=1, le=300)


class RagRuntime:
    def __init__(self, store, config):
        destinations = {}
        for entry in config.destinations:
            if entry.key in destinations:
                raise ValueError("duplicate destination key")
            destinations[entry.key] = Destination(**entry.model_dump(exclude={"key", "graph_sensitivity_ceiling"}))
        outbox = RagOutbox(store, DeliveryPolicy(**config.policy))
        self.worker = RagDeliveryWorker(outbox, destinations.values(),
            interval_seconds=config.interval_seconds, batch_size=config.batch_size,
            warning_seconds=config.warning_seconds)
        self.service = RagApplication(outbox, destinations, self.worker,
                                      {entry.key: entry.graph_sensitivity_ceiling for entry in config.destinations})
        self.shutdown_seconds = config.shutdown_seconds

    def start(self):
        self.worker.start()

    def stop(self):
        if not self.worker.stop(self.shutdown_seconds):
            # The application's runtime lease must stay held while this writer lives.
            raise ApplicationError(503, "rag_shutdown_pending", "Delivery worker has not stopped.")


def configured_runtime(store, path):
    if path is None:
        return None
    try:
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("absolute configuration path required")
        with path.open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError("configuration too large")
        return RagRuntime(store, RuntimeConfig.model_validate_json(raw))
    except Exception:
        # Do not leak Pydantic input values containing operator credentials.
        raise ApplicationError(503, "rag_configuration_invalid", "RAG configuration is invalid.") from None
