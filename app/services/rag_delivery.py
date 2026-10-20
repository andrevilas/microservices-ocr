"""Staged OCR->RAG transport. Not wired to routes, workers or retention."""

import base64
import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx2

from app.application import ApplicationError, Principal
from app.results import read_page, read_report
from app.services.rag_delivery_contract import (
    DeliveryReceipt, match_receipt,
    parse_manifest, parse_receipt,
)

MAX_ARTIFACTS = 8 * 1024 * 1024
MAX_PAGES = 2 * 1024 * 1024
MAX_RECEIPT = 65536


@dataclass(frozen=True)
class Destination:
    """Operator-owned binding, never constructed from an untrusted request."""
    url: str
    destination_id: str
    owner: str
    source_kind: str
    process_reference: str | None = None
    token: str = field(default="", repr=False, compare=False)

    def validate(self):
        try:
            if not isinstance(self.url, str) or any(ord(c) <= 32 for c in self.url):
                raise ValueError("invalid url")
            parsed = urlsplit(self.url)
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError("invalid port")
        except (ValueError, TypeError):
            raise ApplicationError(503, "rag_configuration_invalid", "RAG destination is not configured safely.") from None
        if (parsed.username or parsed.password or parsed.query or parsed.fragment
                or not parsed.hostname or parsed.path not in ("", "/")
                or (parsed.scheme != "https"
                    and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "::1", "localhost")))
                or not isinstance(self.token, str) or not 32 <= len(self.token) <= 256
                or any(ord(c) < 33 or ord(c) > 126 for c in self.token)):
            raise ApplicationError(503, "rag_configuration_invalid", "RAG destination is not configured safely.")


@dataclass(frozen=True)
class PreparedDelivery:
    payload: bytes
    # Immutable bytes are authoritative; reconstruct manifest at every send.
    owner: str


@dataclass(frozen=True)
class DeliveryResult:
    receipt: DeliveryReceipt
    indexed: bool
    # No deletion/hold-release permission exists in this transport result.


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def prepare_delivery(store, principal: Principal, job_id: str,
                     destination: Destination) -> PreparedDelivery:
    principal.require("jobs:read")
    principal.require("artifacts:read")
    destination.validate()
    if destination.owner != principal.subject:
        raise ApplicationError(403, "rag_destination_forbidden", "RAG destination is not assigned to this owner.")
    with store.artifact_lease(job_id, principal.subject) as job:
        if job.status != "completed" or job.output_pdf_path is None:
            raise ApplicationError(409, "result_not_ready", "Completed OCR result is required.")
        try:
            report = read_report(job.working_dir)
            if report.job_id != job_id:
                raise ValueError("report mismatch")
            with job.output_pdf_path.open("rb") as stream:
                pdf = stream.read(MAX_ARTIFACTS + 1)
            if len(pdf) > MAX_ARTIFACTS:
                raise ApplicationError(413, "rag_package_limit", "Result exceeds the RAG pilot limit.")
            if (len(pdf) != report.output.size_bytes
                    or hashlib.sha256(pdf).hexdigest() != report.output.sha256):
                raise ValueError("output mismatch")
            page_lines = []
            page_bytes = 0
            for info in report.pages:
                text = read_page(job.working_dir, info)
                # Preserve blank pages so subsequent citations keep PDF numbering.
                if len(text) > 200000 or "\x00" in text:
                    raise ApplicationError(422, "rag_page_unsupported", "A page cannot be represented by the RAG pilot.")
                line = _json({"page": info.page, "text": text}) + b"\n"
                page_bytes += len(line)
                if page_bytes > MAX_PAGES or page_bytes + len(pdf) > MAX_ARTIFACTS:
                    raise ApplicationError(413, "rag_package_limit", "Result exceeds the RAG pilot limit.")
                page_lines.append(line)
            pages = b"".join(page_lines)
            raw = {"pdf": pdf, "pages": pages}
            descriptors = [
                {"role": role, "sha256": hashlib.sha256(body).hexdigest(), "size_bytes": len(body),
                 "media_type": "application/pdf" if role == "pdf" else "application/x-ndjson"}
                for role, body in raw.items()]
            data = dict(
                contract_version="ocr-rag/1", delivery_id="pending", destination_id=destination.destination_id,
                job_id=job_id, source_revision=report.output.sha256, source_kind=destination.source_kind,
                process_reference=destination.process_reference, metadata_state="absent", artifacts=descriptors)
            # Bind retries to owner, full source and destination classification.
            data["delivery_id"] = hashlib.sha256(_json({"owner": principal.subject, "manifest": data})).hexdigest()
            manifest = parse_manifest(_json(data))
            payload = _json({"manifest": manifest.model_dump(mode="json"),
                             "artifacts": {role: base64.b64encode(body).decode("ascii") for role, body in raw.items()}})
            return PreparedDelivery(payload=payload, owner=principal.subject)
        except ApplicationError:
            raise
        except (ValueError, OSError):
            raise ApplicationError(503, "rag_source_integrity", "OCR result could not be verified.") from None


def send_delivery(delivery: PreparedDelivery, destination: Destination, *, transport=None) -> DeliveryResult:
    """One attempt only. Retry scheduling and durable holds belong to the outbox."""
    destination.validate()
    if delivery.owner != destination.owner:
        raise ApplicationError(403, "rag_destination_forbidden", "RAG destination is not assigned to this owner.")
    try:
        if not isinstance(delivery.payload, bytes) or len(delivery.payload) > 12 * 1024 * 1024:
            raise ValueError("invalid package")
        body = json.loads(delivery.payload)
        manifest = parse_manifest(_json(body["manifest"]))
        if (manifest.destination_id != destination.destination_id
                or manifest.source_kind != destination.source_kind
                or manifest.process_reference != destination.process_reference):
            raise ValueError("destination mismatch")
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ApplicationError(422, "rag_package_invalid", "Prepared delivery does not match the configured destination.") from None
    try:
        with httpx2.Client(timeout=30, follow_redirects=False, trust_env=False,
                           transport=transport) as client:
            with client.stream("POST", destination.url.rstrip("/") + "/v1/deliveries",
                               content=delivery.payload,
                               headers={"Authorization": "Bearer " + destination.token,
                                        "Content-Type": "application/json", "Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    retryable = response.status_code in (408, 429, 500, 502, 503, 504)
                    raise ApplicationError(503 if retryable else 409,
                                           "rag_delivery_uncertain" if retryable else "rag_delivery_rejected",
                                           "RAG did not acknowledge delivery.")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("encoded receipt")
                receipt_bytes = bytearray()
                for chunk in response.iter_raw(chunk_size=4096):
                    if len(receipt_bytes) + len(chunk) > MAX_RECEIPT:
                        raise ValueError("receipt limit")
                    receipt_bytes.extend(chunk)
                receipt = parse_receipt(bytes(receipt_bytes))
                indexed = match_receipt(manifest, receipt)
                return DeliveryResult(receipt=receipt, indexed=indexed)
    except httpx2.HTTPError:
        raise ApplicationError(503, "rag_delivery_uncertain", "RAG acknowledgement is unavailable; reconcile before retry.") from None
    except (ValueError, RecursionError):
        raise ApplicationError(502, "rag_receipt_invalid", "RAG receipt failed verification.") from None
