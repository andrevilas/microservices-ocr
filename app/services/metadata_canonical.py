"""Full canonical pages for metadata evidence within a caller-owned lease.

The caller keeps CanonicalTextService.open active through consumption and any
 eventual commit. This adapter grants no save or approval authority, opens no
lease, and caches no text. assess_snapshot owns its operation-local page cache.
Manifest PDF hashes are persisted report identifiers, not fresh PDF hashes or
PDF/A validation results.
"""

from app.metadata_evidence import CanonicalPageText
from app.services.canonical_text_service import PreparedCanonicalText


class MetadataCanonicalSource:
    """Adapt one live preparation; failures propagate without losing integrity errors."""

    def __init__(self, prepared: PreparedCanonicalText):
        self._manifest = dict(prepared.manifest)
        self._prepared = prepared

    def __call__(self, page: int) -> CanonicalPageText:
        # Delegate first so a closed preparation fails even for an invalid page.
        full = self._prepared.read_page_text(page)
        return CanonicalPageText(
            document_id=self._manifest["document_id"],
            artifact_version=self._manifest["artifact_version"],
            text_version=self._manifest["text_version"],
            canonical_sha256=self._manifest["sha256"],
            page=full["page"],
            text_sha256=full["text_sha256"],
            text=full["text"],
        )
