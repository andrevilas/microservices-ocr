"""Explicit opt-in runtime from a bounded operator configuration file."""
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from app.application import ApplicationError
from app.services.rag_application import RagApplication
from app.services.rag_delivery import Destination
from app.services.rag_outbox import DeliveryPolicy, RagOutbox
from app.services.rag_worker import RagDeliveryWorker


class BindingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(pattern="^[a-zA-Z0-9_-]{1,64}$")
    url: str
    destination_id: str
    owner: str
    source_kind: str
    process_reference: str | None = None
    token: str = Field(repr=False)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy: dict[str, int]
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
            destinations[entry.key] = Destination(**entry.model_dump(exclude={"key"}))
        outbox = RagOutbox(store, DeliveryPolicy(**config.policy))
        self.service = RagApplication(outbox, destinations)
        self.worker = RagDeliveryWorker(outbox, destinations.values(),
            interval_seconds=config.interval_seconds, batch_size=config.batch_size,
            warning_seconds=config.warning_seconds)
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
