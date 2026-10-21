"""Independent, literal expectations for the currently advertised MCP tools."""

EXPECTED_TOOL_COUNT = 58
EXPECTED_TOOL_NAMES = frozenset({
    "ocr_list_rag_destinations", "ocr_submit_rag_delivery", "ocr_get_rag_delivery",
    "ocr_get_rag_receipt", "ocr_get_rag_recovery", "ocr_cancel_rag_delivery",
    "ocr_list_rag_deliveries", "ocr_get_rag_operations", "ocr_retry_rag_delivery",
    "ocr_list_context_templates",
    "ocr_clone_context_template",
    "ocr_whoami",
    "ocr_list_jobs",
    "ocr_get_job",
    "ocr_get_metrics",
    "ocr_get_artifact",
    "ocr_get_text",
    "ocr_get_report",
    "ocr_get_capabilities",
    "ocr_prepare_upload",
    "ocr_get_upload",
    "ocr_submit_job",
    "ocr_create_batch",
    "ocr_list_batches",
    "ocr_get_batch",
    "ocr_create_export",
    "ocr_get_export",
    "ocr_cancel_job",
    "ocr_cancel_jobs",
    "ocr_retry_job",
    "ocr_delete_job_artifacts",
    "ocr_list_contexts",
    "ocr_create_context",
    "ocr_get_context",
    "ocr_update_context",
    "ocr_list_document_types",
    "ocr_create_document_type",
    "ocr_get_document_type",
    "ocr_update_document_type",
    "ocr_publish_document_type",
    "ocr_list_document_type_versions",
    "ocr_get_document_type_version",
    "ocr_deprecate_document_type_version",
    "ocr_get_document_canonical_manifest",
    "ocr_get_document_canonical_chunk",
    "ocr_get_document_metadata",
    "ocr_list_document_metadata_revisions",
    "ocr_get_document_metadata_revision",
    "ocr_list_document_metadata_fields",
    "ocr_list_document_metadata_values",
    "ocr_list_document_metadata_evidence",
    "ocr_get_document_metadata_string",
    "ocr_get_document_metadata_schema",
    "ocr_save_document_metadata",
    "ocr_approve_document_metadata",
})

CATALOG_READ_TOOL_NAMES = frozenset({
    "ocr_list_context_templates",
    "ocr_list_contexts",
    "ocr_get_context",
    "ocr_list_document_types",
    "ocr_get_document_type",
    "ocr_list_document_type_versions",
    "ocr_get_document_type_version",
})
CATALOG_WRITE_TOOL_NAMES = frozenset({
    "ocr_clone_context_template",
    "ocr_create_context",
    "ocr_update_context",
    "ocr_create_document_type",
    "ocr_update_document_type",
    "ocr_publish_document_type",
    "ocr_deprecate_document_type_version",
})
CATALOG_UPDATE_TOOL_NAMES = frozenset({
    "ocr_update_context",
    "ocr_update_document_type",
})
CATALOG_DEPRECATE_TOOL_NAMES = frozenset({
    "ocr_deprecate_document_type_version",
})
OTHER_WRITE_TOOL_NAMES = frozenset({
    "ocr_submit_rag_delivery", "ocr_cancel_rag_delivery", "ocr_retry_rag_delivery",
    "ocr_prepare_upload",
    "ocr_submit_job",
    "ocr_create_batch",
    "ocr_create_export",
    "ocr_cancel_job",
    "ocr_cancel_jobs",
    "ocr_retry_job",
    "ocr_delete_job_artifacts",
    "ocr_save_document_metadata",
    "ocr_approve_document_metadata",
})


def assert_tool_inventory(tools):
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)) == EXPECTED_TOOL_COUNT
    assert set(names) == EXPECTED_TOOL_NAMES


def assert_catalog_annotations(tools):
    writes = OTHER_WRITE_TOOL_NAMES | CATALOG_WRITE_TOOL_NAMES
    assert all(tool.annotations.read_only_hint == (tool.name not in writes) for tool in tools)
    by_name = {tool.name: tool for tool in tools}
    for name in CATALOG_READ_TOOL_NAMES | CATALOG_WRITE_TOOL_NAMES:
        hints = by_name[name].annotations
        assert hints.open_world_hint is False
        assert hints.destructive_hint is (name in CATALOG_UPDATE_TOOL_NAMES | CATALOG_DEPRECATE_TOOL_NAMES)
        assert hints.idempotent_hint is (name not in CATALOG_UPDATE_TOOL_NAMES)
