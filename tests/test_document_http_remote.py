"""Synthetic OAuth document HTTP coverage; no browser or live identity provider."""
import asyncio
import hashlib
import inspect
import json
import os
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2
import pytest

from app.application import JobApplication
from app.remote import RemoteConfig, create_remote_app
from app.scopes import OAUTH_SCOPES
from test_document_metadata import PAGES, QUOTE, body, env, persisted
from test_document_remote_scopes import claims, verifier_for
from test_remote_oauth import config

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")


@asynccontextmanager
async def remote(config, e):
    cutoff = time.time() - 1000
    subjects = {name: {"user_id": uid, "scopes": sorted(OAUTH_SCOPES),
                       "document_scopes_not_before": cutoff}
                for name, uid in (("subject-one", 1), ("subject-two", 2))}
    full = RemoteConfig(config.origin, config.issuer, config.introspection_client_id,
                        config.secret_file, subjects, config.allowed_clients, config.ca_file)
    token = claims(full)
    verifier, users = verifier_for(full, token)
    app = create_remote_app(full, verifier=verifier, service_provider=lambda: JobApplication(e.jobs))
    try:
        await verifier.start()
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                     base_url=full.origin) as client:
            yield client, verifier, token, users, full, cutoff
    finally:
        try:
            # Drain the actual main app's document provider before env closes SQLite.
            for provider in app.app.state._state.values():
                if type(provider).__module__ != "app.services.document_runtime":
                    continue
                drain = next((getattr(provider, name) for name in ("drain", "close", "shutdown")
                              if callable(getattr(provider, name, None))), None)
                if drain is not None:
                    result = await asyncio.to_thread(drain)
                    if inspect.isawaitable(result):
                        await result
        finally:
            await verifier.close()


def test_document_remote_exact_route_gates(config, env, monkeypatch):
    async def run():
        async with remote(config, env) as (client, verifier, *_):
            attempts = []
            authenticate = verifier.authenticate
            async def counted(request):
                attempts.append(request.url.path)
                return await authenticate(request)
            monkeypatch.setattr(verifier, "authenticate", counted)
            root = "/api/v1/jobs/" + "a" * 32
            revision = "/metadata/revisions/" + "b" * 32
            fields = revision + "/segments/" + "c" * 32 + "/fields"
            routes = [("GET", suffix) for suffix in (
                "/metadata", "/metadata/revisions", revision, revision + "/content", fields,
                fields + "/title/values", fields + "/title/evidence",
                fields + "/title/strings/raw", revision + "/schemas/" + "d" * 64,
                "/canonical/manifest", "/canonical/pages", "/canonical/pages/1",
                "/canonical/pages.jsonl")]
            routes += [("PUT", "/metadata"), ("POST", "/metadata/approve")]
            assert len(routes) == 15
            for method, suffix in routes:
                response = await client.request(method, root + suffix)
                assert response.status_code == 401, (method, suffix, response.text)
            for suffix in (fields + "/title/strings/normalized", "/canonical/pages/999"):
                assert (await client.get(root + suffix)).status_code == 401
            before = len(attempts)
            denied = [("DELETE", root + "/metadata"), ("POST", root + "/metadata"),
                      ("PUT", root + "/metadata/approve"), ("GET", root + "/metadata/approve"),
                      ("GET", "/api/v1/admin"), ("GET", "/api/v1/accounts"),
                      ("GET", root + "/metadata/future"), ("GET", root + revision + "/future"),
                      ("GET", root + "/canonical/pagesXjsonl"),
                      ("GET", root + "/canonical/pages/0"), ("GET", root + "/canonical/pages/1000"),
                      ("GET", root + "/canonical/pages/01"),
                      ("GET", root.upper() + "/metadata"),
                      ("GET", root[:-1] + "/metadata"),
                      ("GET", root + "/metadata/revisions/" + "B" * 32),
                      ("GET", root + revision + "/segments/" + "c" * 31 + "/fields"),
                      ("GET", root + revision + "/schemas/" + "D" * 64),
                      ("GET", root + revision + "/schemas/" + "d" * 63)]
            denied += [("GET", root + fields + "/" + name + "/values")
                       for name in ("Title", "1title", "a" * 65, "bad-name")]
            denied += [("GET", root + fields + "/title/strings/other")]
            for method, path in denied:
                response = await client.request(method, path, headers={"Authorization": "Bearer synthetic-token"})
                assert response.status_code == 404, (method, path, response.text)
            assert len(attempts) == before
    asyncio.run(run())


def test_document_remote_oauth_vertical_flow(config, env):
    async def run():
        e = env
        async with remote(config, e) as (client, verifier, token, users, full, cutoff):
            baseline = dict(token)
            def reset(**changes):
                token.clear()
                token.update(baseline | changes)
            client.headers["Authorization"] = "Bearer synthetic-token"
            root = f"/api/v1/jobs/{e.job.job_id}"
            response = await client.put(root + "/metadata", json=body(e),
                                        headers={"Idempotency-Key": "remote-document-save-0001"})
            assert response.status_code == 201, response.text
            receipt = SimpleNamespace(**response.json())
            snapshot = persisted(e, receipt)
            response = await client.get(root + "/metadata")
            assert response.status_code == 200, response.text
            assert response.json()["revision_id"] == receipt.revision_id
            assert response.json()["sha256"] == receipt.sha256
            segment = snapshot.segments[0]
            path = root + f"/metadata/revisions/{receipt.revision_id}/segments/{segment.segment_id}/fields/title/values"
            response = await client.get(path, params={"sha256": receipt.sha256})
            assert response.status_code == 200 and QUOTE in response.text
            async def canonical():
                response = await client.get(root + "/canonical/pages.jsonl")
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["x-content-sha256"] == hashlib.sha256(response.content).hexdigest()
                assert [json.loads(line)["text"] for line in response.content.splitlines()] == PAGES
            await canonical()
            approval = dict(revision_id=receipt.revision_id, expected_head=receipt.revision_id,
                            expected_sha256=receipt.sha256,
                            reviewed_fields=[dict(segment_id=s.segment_id, field_name=f.field_name)
                                             for s in snapshot.segments for f in s.fields])
            reset(scope="jobs:read artifacts:read metadata:read metadata:approve")
            headers = {"Idempotency-Key": "remote-document-approve-0001"}
            response = await client.post(root + "/metadata/approve", json=approval, headers=headers)
            assert response.status_code == 201, response.text
            approved = response.json()
            response = await client.post(root + "/metadata/approve", json=approval, headers=headers)
            assert response.status_code == 200 and response.json() == approved
            assert response.headers["idempotency-replayed"] == "true"
            assert (await client.put(root + "/metadata", content=b"{", headers={
                "Content-Type": "application/json", "Idempotency-Key": "remote-denied-save-0001"})).status_code == 403
            reset(scope="jobs:read artifacts:read")
            await canonical()
            assert (await client.get(root + "/metadata")).status_code == 403
            reset(iat=cutoff - 1)
            await canonical()
            assert (await client.get(root + "/metadata")).status_code == 403
            users[2] = {"id": 2, "role": "admin"}
            reset(sub="subject-two", roles=["admin"])
            assert (await client.get(root + "/metadata")).status_code == 404
            assert (await client.get(root + "/canonical/pages.jsonl")).status_code == 404
            reset()
    asyncio.run(run())
