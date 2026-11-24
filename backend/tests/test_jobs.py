"""The Jobs page backend: Fabric client, chain runner, registry and endpoints.

No test reaches Fabric, Entra or Neo4j: Fabric is a scripted ``httpx.MockTransport`` and the mapping job is faked.
"""

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.chat.llm import LLMNotConfigured
from app.config import Settings
from app.jobs import registry
from app.jobs.fabric import FabricClient, FabricError, FabricNotConfigured, TokenProvider, build_parameters
from app.jobs.registry import CHAINS, JOBS, JOBS_BY_KEY, coerce_params
from app.jobs.router import FabricHub, JobsDeps
from app.jobs.runs import ItemResolver, JobStore, RunState, StepState, execute_chain, new_chain_run
from app.main import create_app
from app.mapping import service as mapping_service
from app.snapshot import SnapshotRepository

ROOT = Path(__file__).resolve().parents[2]
WS = "11111111-aaaa-bbbb-cccc-222222222222"


def guid(n: int) -> str:
    """Job instance ids are GUIDs in Fabric, and the client only accepts that shape."""
    return f"{n:08x}-1111-4222-8333-444444444444"


INST1, INST_BAD = guid(1), guid(9999)


def jwt(exp: float) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


def cfg(**kw: Any) -> Settings:
    base = dict(
        mode="snapshot", static_dir=ROOT / "__no_frontend__", fabric_workspace_id=WS, fabric_workspace_name="", fabric_tenant_id="",
        fabric_client_id="", fabric_client_secret="", fabric_access_token=jwt(time.time() + 3600), fabric_poll_seconds=0,
        fabric_api_url="https://fabric.test", fabric_login_url="https://login.test", fabric_param_style="array", mapping_ui_write=True,
    )
    return Settings(**{**base, **kw})


class FakeFabric:
    """A stand-in for the Fabric REST API that records every request and plays back scripted job states."""

    def __init__(self, items: dict[str, list[str]] | None = None, scripts: dict[str, list[str]] | None = None, page_size: int = 100):
        self.items = {t: [{"id": f"id-{n}", "displayName": n, "type": t} for n in names] for t, names in (items or {}).items()}
        self.scripts = scripts or {}  # display name -> statuses returned by successive polls
        self.page_size = page_size
        self.requests: list[httpx.Request] = []
        self.posted: list[dict[str, Any]] = []
        self.instances: dict[str, str] = {}  # instance id -> item display name
        self.polls: dict[str, int] = {}
        self.token_calls = 0
        self.rate_limit_next = 0
        self.fail_with: int | None = None
        self.bad_location: str | None = None

    def client(self, settings: Settings) -> FabricClient:
        return FabricClient(settings, http=httpx.Client(transport=httpx.MockTransport(self.handle)), sleep=lambda s: None)

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path, q = req.url.path, dict(req.url.params)
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"errorCode": "TooManyRequests"})
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"errorCode": "Nope", "message": "refused"})
        if req.url.host == "login.test":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_calls}", "expires_in": 3600})
        if path == "/v1/workspaces":
            return httpx.Response(200, json={"value": [{"id": WS, "displayName": "Ontology_Lineage"}, {"id": "other", "displayName": "Other"}]})
        if path == f"/v1/workspaces/{WS}/items" and req.method == "GET":
            rows = self.items.get(q["type"], [])
            start = int(q.get("continuationToken", 0))
            page = rows[start : start + self.page_size]
            body: dict[str, Any] = {"value": page}
            if start + self.page_size < len(rows):
                body["continuationToken"] = str(start + self.page_size)
            return httpx.Response(200, json=body)
        if path.endswith("/jobs/instances") and req.method == "POST":
            item_id = path.split("/")[-3]  # .../items/{item_id}/jobs/instances
            self.posted.append({"item_id": item_id, "jobType": q.get("jobType"), "body": json.loads(req.content) if req.content else None})
            inst = guid(len(self.instances) + 1)
            self.instances[inst] = item_id.removeprefix("id-")
            loc = self.bad_location if self.bad_location is not None else f"https://fabric.test/v1/workspaces/{WS}/items/{item_id}/jobs/instances/{inst}"
            return httpx.Response(202, headers={"Location": loc, "Retry-After": "7"})
        if "/jobs/instances/" in path and path.endswith("/cancel"):
            self.posted.append({"cancel": path.split("/")[-2]})
            return httpx.Response(202)
        if "/jobs/instances/" in path and req.method == "GET":
            inst = path.split("/")[-1]
            name = self.instances[inst]
            script = self.scripts.get(name, ["Completed"])
            n = self.polls.get(inst, 0)
            self.polls[inst] = n + 1
            status = script[min(n, len(script) - 1)]
            body = {"id": inst, "status": status, "startTimeUtc": "2026-11-23T10:00:00", "endTimeUtc": None,
                    "failureReason": {"errorCode": "Spark", "message": "Cell 4 raised KeyError"} if status == "Failed" else None}
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"errorCode": "NotFound"})


ITEMS = {"Notebook": ["policy_docs_chunk_step2", "policy_chunks_to_neo4j_vector_step3", "embed_lineage_nodes_to_neo4j_step4", "nb_llm_policy_mapping"], "DataPipeline": ["PL_CP_Accounts"]}


# --- registry ---------------------------------------------------------------------------------------


def test_registry_is_consistent() -> None:
    assert len({j.key for j in JOBS}) == len(JOBS) and len({c.key for c in CHAINS}) == len(CHAINS)
    for c in CHAINS:
        assert c.steps and all(k in JOBS_BY_KEY for k in c.steps), c.key
    for j in JOBS:
        assert j.names and j.item_type in (registry.NOTEBOOK, registry.PIPELINE) and j.group in registry.GROUPS, j.key
        assert j.fabric_job_type == ("RunNotebook" if j.item_type == registry.NOTEBOOK else "Pipeline")
    assert all(g in registry.GROUPS for g in {c.group for c in CHAINS})


def test_set_a_demo_order_matches_the_handoff() -> None:
    chains = {c.key: c.steps for c in CHAINS}
    # step 5 is retired (a no-op stub), so no chain runs it any more
    assert chains["a_demo"] == ("policy_chunk", "policy_load_graph", "lineage_embed")  # steps 2, 3, 4
    assert chains["a_policy_changed"] == ("policy_chunk", "policy_load_graph")  # 2, 3
    assert "a_lineage_changed" not in chains and not any("policy_map_legacy" in s for s in chains.values())
    assert chains["structured_all"] == ("pl_accounts", "pl_branches", "pl_customers", "pl_loans", "pl_txn")
    assert chains["b_first_run"] == ("b01", "b02", "b03", "b05", "b04", "b06")
    assert chains["b_policies"] == ("b02", "b03", "b04", "b06") and chains["b_data"][0] == "lineage_extract"


def test_risky_jobs_demand_confirmation_and_others_do_not() -> None:
    assert all(JOBS_BY_KEY[k].confirm for k in ("policy_map_legacy", "lineage_extract", "pl_accounts", "pl_branches", "pl_customers", "pl_loans", "pl_txn"))
    assert not any(JOBS_BY_KEY[k].confirm for k in ("policy_chunk", "policy_load_graph", "lineage_embed", "llm_mapping_nb"))


def test_coerce_params_types_and_refuses_the_rest() -> None:
    job = JOBS_BY_KEY["llm_mapping_nb"]
    assert coerce_params(job, {"MODE": "overwrite", "WRITE": True, "LIMIT": 5, "DOCS": "loan"}) == {"MODE": "overwrite", "WRITE": True, "LIMIT": 5, "DOCS": "loan"}
    assert coerce_params(job, None) == {}
    for bad in ({"NOPE": 1}, {"WRITE": "yes"}, {"LIMIT": -1}, {"LIMIT": True}, {"MODE": "merge"}, {"DOCS": "x" * 501}, {"LIMIT": 10**9}):
        with pytest.raises(ValueError):
            coerce_params(job, bad)
    with pytest.raises(ValueError):
        coerce_params(JOBS_BY_KEY["policy_chunk"], {"MODE": "append"})  # this job takes no parameters


# --- sign-in ----------------------------------------------------------------------------------------


def test_service_principal_token_is_requested_once_and_cached() -> None:
    fake = FakeFabric()
    s = cfg(fabric_access_token="", fabric_tenant_id="tenant-1", fabric_client_id="cid", fabric_client_secret="s3cret")
    now = [1000.0]
    tp = TokenProvider(s, httpx.Client(transport=httpx.MockTransport(fake.handle)), clock=lambda: now[0])
    assert tp.mode == "service_principal" and tp.token() == "tok1" and tp.token() == "tok1" and fake.token_calls == 1
    req = fake.requests[0]
    assert req.url.path == "/tenant-1/oauth2/v2.0/token"
    form = dict(httpx.QueryParams(req.content.decode()))
    assert form["grant_type"] == "client_credentials" and form["scope"] == "https://api.fabric.microsoft.com/.default" and form["client_id"] == "cid"
    assert tp.expires_in() == 3600
    now[0] += 3600 - 30  # inside the 60 second safety margin
    assert tp.token() == "tok2" and fake.token_calls == 2


def test_a_failed_sign_in_never_leaks_the_secret() -> None:
    def deny(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret.\r\nTrace ID: x"})

    s = cfg(fabric_access_token="", fabric_tenant_id="t", fabric_client_id="cid", fabric_client_secret="TOP-SECRET-VALUE")
    with pytest.raises(FabricError) as err:
        TokenProvider(s, httpx.Client(transport=httpx.MockTransport(deny))).token()
    assert "TOP-SECRET-VALUE" not in str(err.value) and "AADSTS7000215" in str(err.value) and err.value.status_code == 401


def test_pasted_token_expiry_is_read_and_enforced() -> None:
    http = httpx.Client(transport=httpx.MockTransport(FakeFabric().handle))
    live = TokenProvider(cfg(fabric_access_token=jwt(time.time() + 600)), http)
    assert live.mode == "token" and 590 <= live.expires_in() <= 600 and live.token()
    dead = TokenProvider(cfg(fabric_access_token=jwt(time.time() - 5)), http)
    with pytest.raises(FabricError, match="expired"):
        dead.token()
    assert TokenProvider(cfg(fabric_access_token="not-a-jwt"), http).expires_in() is None  # unreadable: just try it


def test_no_credentials_gives_instructions() -> None:
    tp = TokenProvider(cfg(fabric_access_token=""), httpx.Client(transport=httpx.MockTransport(FakeFabric().handle)))
    assert tp.mode == "none"
    with pytest.raises(FabricNotConfigured, match="FABRIC_ACCESS_TOKEN"):
        tp.token()
    assert FakeFabric().client(cfg(fabric_access_token="")).configured is False
    assert FakeFabric().client(cfg()).configured is True
    assert FakeFabric().client(cfg(fabric_workspace_id="", fabric_workspace_name="")).configured is False


# --- the client -------------------------------------------------------------------------------------


def test_parameter_shapes() -> None:
    p = {"MODE": "append", "WRITE": False, "LIMIT": 5, "RATIO": 0.5}
    assert build_parameters(p, "array") == {"parameters": [
        {"name": "MODE", "value": "append", "type": "Text"}, {"name": "WRITE", "value": False, "type": "Boolean"},
        {"name": "LIMIT", "value": 5, "type": "Number"}, {"name": "RATIO", "value": 0.5, "type": "Number"}]}
    assert build_parameters(p, "execution_data") == {"executionData": {"parameters": {
        "MODE": {"value": "append", "type": "string"}, "WRITE": {"value": False, "type": "bool"},
        "LIMIT": {"value": 5, "type": "int"}, "RATIO": {"value": 0.5, "type": "float"}}}}
    assert build_parameters({}, "array") == {}


def test_workspace_is_found_by_name_or_taken_by_id() -> None:
    fake = FakeFabric()
    by_id = fake.client(cfg())
    assert by_id.workspace_id() == WS and not fake.requests
    by_name = fake.client(cfg(fabric_workspace_id="", fabric_workspace_name="ontology_lineage"))  # case-insensitive
    assert by_name.workspace_id() == WS and by_name.workspace_id() == WS and len(fake.requests) == 1  # looked up once
    with pytest.raises(FabricError, match="No workspace named 'Missing'"):
        fake.client(cfg(fabric_workspace_id="", fabric_workspace_name="Missing")).workspace_id()


def test_list_items_follows_continuation_tokens() -> None:
    fake = FakeFabric(items={"Notebook": [f"nb{i}" for i in range(7)]}, page_size=3)
    got = fake.client(cfg()).list_items("Notebook")
    assert [i["displayName"] for i in got] == [f"nb{i}" for i in range(7)] and len(fake.requests) == 3
    assert fake.requests[0].url.params["type"] == "Notebook"
    assert all(r.headers["authorization"].startswith("Bearer ") for r in fake.requests)


def test_run_posts_the_job_and_returns_the_instance() -> None:
    fake = FakeFabric(items=ITEMS)
    client = fake.client(cfg())
    handle = client.run("id-nb_llm_policy_mapping", "RunNotebook", {"MODE": "append", "WRITE": False})
    assert handle.instance_id == INST1 and handle.retry_after == 7.0
    assert fake.posted[0] == {"item_id": "id-nb_llm_policy_mapping", "jobType": "RunNotebook", "body": {"parameters": [
        {"name": "MODE", "value": "append", "type": "Text"}, {"name": "WRITE", "value": False, "type": "Boolean"}]}}
    client.run("id-PL_CP_Accounts", "Pipeline")
    assert fake.posted[1]["body"] is None and fake.posted[1]["jobType"] == "Pipeline"  # no parameters, no body


def test_the_token_is_never_sent_to_an_address_the_server_chose() -> None:
    fake = FakeFabric(items=ITEMS)
    fake.bad_location = f"https://evil.example/steal/{INST_BAD}"
    client = fake.client(cfg())
    handle = client.run("id-nb_llm_policy_mapping", "RunNotebook")
    assert handle.instance_id == INST_BAD
    fake.instances[INST_BAD] = "nb_llm_policy_mapping"
    client.status("id-nb_llm_policy_mapping", handle.instance_id)
    assert {r.url.host for r in fake.requests} == {"fabric.test"}  # the status URL was rebuilt, not followed


def test_a_run_without_a_job_instance_is_an_error() -> None:
    fake = FakeFabric(items=ITEMS)
    fake.bad_location = ""
    with pytest.raises(FabricError, match="no job instance"):
        fake.client(cfg()).run("id-x", "RunNotebook")


def test_status_and_cancel() -> None:
    fake = FakeFabric(items=ITEMS, scripts={"nb_llm_policy_mapping": ["InProgress", "Failed"]})
    client = fake.client(cfg())
    h = client.run("id-nb_llm_policy_mapping", "RunNotebook")
    assert client.status("id-nb_llm_policy_mapping", h.instance_id)["status"] == "InProgress"
    failed = client.status("id-nb_llm_policy_mapping", h.instance_id)
    assert failed["status"] == "Failed" and failed["failure"] == "Cell 4 raised KeyError"
    client.cancel("id-nb_llm_policy_mapping", h.instance_id)
    assert fake.posted[-1] == {"cancel": h.instance_id}


def test_rate_limits_wait_and_retry_then_succeed() -> None:
    fake = FakeFabric(items=ITEMS)
    waits: list[float] = []
    client = FabricClient(cfg(), http=httpx.Client(transport=httpx.MockTransport(fake.handle)), sleep=waits.append)
    fake.rate_limit_next = 2
    assert client.list_items("Notebook") and waits == [2.0, 2.0]
    fake.rate_limit_next = 99
    with pytest.raises(FabricError, match="rate limiting") as err:
        client.list_items("Notebook")
    assert err.value.status_code == 429


@pytest.mark.parametrize("status,needle", [(401, "sign-in"), (403, "Contributor"), (404, "could not find"), (500, "HTTP 500")])
def test_errors_are_translated_and_carry_no_secrets(status, needle) -> None:
    fake = FakeFabric(items=ITEMS)
    fake.fail_with = status
    secret = jwt(time.time() + 3600)
    with pytest.raises(FabricError) as err:
        fake.client(cfg(fabric_access_token=secret)).list_items("Notebook")
    assert needle.lower() in str(err.value).lower() and secret not in str(err.value) and "Bearer" not in str(err.value)
    assert err.value.status_code == status


def test_an_unreachable_fabric_is_reported_not_crashed() -> None:
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(FabricError, match="Could not reach Fabric"):
        FabricClient(cfg(), http=httpx.Client(transport=httpx.MockTransport(down))).list_items("Notebook")


# --- the chain runner -------------------------------------------------------------------------------


def run_chain(fake: FakeFabric, keys: list[str], settings: Settings | None = None, params: dict | None = None, clock=None, mutate=None) -> tuple[JobStore, RunState]:  # noqa: ANN001
    s = settings or cfg()
    client = fake.client(s)
    store = JobStore(synchronous=True)
    jobs = [(JOBS_BY_KEY[k], (params or {}).get(k, {})) for k in keys]
    run = new_chain_run(store, "chain", "test", "Test chain", jobs)
    kw = {"clock": clock} if clock else {}
    store.start(run, lambda: execute_chain(store, run, client, ItemResolver(client), jobs, s, sleep=(mutate or (lambda sec: None)), **kw))
    return store, run


def test_a_chain_runs_each_step_after_the_last_completes() -> None:
    fake = FakeFabric(items=ITEMS, scripts={"policy_docs_chunk_step2": ["InProgress", "InProgress", "Completed"]})
    store, run = run_chain(fake, ["policy_chunk", "policy_load_graph", "lineage_embed"])
    assert run.status == "succeeded" and [s.status for s in run.steps] == ["Completed"] * 3
    assert [p["item_id"] for p in fake.posted if "item_id" in p] == ["id-policy_docs_chunk_step2", "id-policy_chunks_to_neo4j_vector_step3", "id-embed_lineage_nodes_to_neo4j_step4"]
    assert fake.polls[INST1] == 3  # polled until done, and step 2 only started after step 1 finished
    assert run.finished_at is not None and any("policy_docs_chunk_step2" not in l and "Completed" in l for l in run.log)


def test_a_chain_stops_at_the_first_failure_and_shows_why() -> None:
    fake = FakeFabric(items=ITEMS, scripts={"policy_chunks_to_neo4j_vector_step3": ["InProgress", "Failed"]})
    store, run = run_chain(fake, ["policy_chunk", "policy_load_graph", "lineage_embed"])
    assert run.status == "failed" and [s.status for s in run.steps] == ["Completed", "Failed", "pending"]
    assert "Cell 4 raised KeyError" in run.error and run.steps[1].message == "Cell 4 raised KeyError"
    assert len([p for p in fake.posted if "item_id" in p]) == 2  # step 3 was never started


def test_a_missing_item_fails_clearly_and_starts_nothing() -> None:
    fake = FakeFabric(items={"Notebook": ["policy_docs_chunk_step2"]})
    store, run = run_chain(fake, ["policy_chunk", "policy_load_graph"])
    assert run.status == "failed" and run.steps[1].status == "Failed" and "not found in the workspace" in run.steps[1].message
    assert "policy_chunks_to_neo4j_vector_step3" in run.steps[1].message  # says what it looked for


def test_renamed_items_are_found_by_any_listed_name() -> None:
    fake = FakeFabric(items={"Notebook": ["policy_docs_chunk_and_map_step2"]})  # the old name from the hand-off
    _, run = run_chain(fake, ["policy_chunk"])
    assert run.status == "succeeded" and fake.posted[0]["item_id"] == "id-policy_docs_chunk_and_map_step2"


def test_parameters_reach_fabric() -> None:
    fake = FakeFabric(items=ITEMS)
    run_chain(fake, ["llm_mapping_nb"], params={"llm_mapping_nb": {"MODE": "append", "WRITE": True}})
    assert fake.posted[0]["body"]["parameters"] == [{"name": "MODE", "value": "append", "type": "Text"}, {"name": "WRITE", "value": True, "type": "Boolean"}]


def test_a_deduped_job_is_a_failure_with_an_explanation() -> None:
    fake = FakeFabric(items=ITEMS, scripts={"policy_docs_chunk_step2": ["Deduped"]})
    _, run = run_chain(fake, ["policy_chunk", "policy_load_graph"])
    assert run.status == "failed" and "already running" in run.error and run.steps[1].status == "pending"


def test_cancel_stops_the_chain_after_asking_fabric_once() -> None:
    fake = FakeFabric(items=ITEMS, scripts={"policy_docs_chunk_step2": ["InProgress", "InProgress", "Cancelled"]})
    holder: dict[str, RunState] = {}

    def press_cancel(seconds: float) -> None:
        holder["run"].cancel_requested = True

    client = fake.client(cfg())
    store = JobStore(synchronous=True)
    jobs = [(JOBS_BY_KEY[k], {}) for k in ("policy_chunk", "policy_load_graph")]
    run = holder["run"] = new_chain_run(store, "chain", "t", "T", jobs)
    store.start(run, lambda: execute_chain(store, run, client, ItemResolver(client), jobs, cfg(), sleep=press_cancel))
    assert run.status == "cancelled" and [s.status for s in run.steps] == ["Cancelled", "Cancelled"]
    assert [p for p in fake.posted if "cancel" in p] == [{"cancel": INST1}]  # asked once, not every poll


def test_a_step_that_never_finishes_is_given_up_on() -> None:
    fake = FakeFabric(items=ITEMS, scripts={"policy_docs_chunk_step2": ["InProgress"]})
    ticks = iter(range(0, 100000, 40))
    _, run = run_chain(fake, ["policy_chunk"], settings=cfg(fabric_step_timeout_minutes=1), clock=lambda: float(next(ticks)))
    assert run.status == "failed" and run.steps[0].status == "TimedOut" and "may still be going" in run.error


def test_the_store_keeps_a_bounded_history_and_survives_a_crash() -> None:
    store = JobStore(synchronous=True, keep=3)
    for i in range(6):
        r = store.new("fabric", f"k{i}", f"T{i}")
        r.status = "succeeded"
    assert len(store.runs) == 3 and [r.key for r in store.recent()] == ["k5", "k4", "k3"]
    boom = store.new("fabric", "boom", "Boom")
    store.start(boom, lambda: (_ for _ in ()).throw(RuntimeError("secret detail")))
    assert boom.status == "failed" and "secret detail" not in boom.error and "RuntimeError" in boom.error and boom.finished_at
    running = store.new("chain", "c", "C", [StepState("a", "A", "InProgress"), StepState("b", "B", "pending"), StepState("c", "C", "Completed")])
    running.status = "running"
    assert store.busy_keys() == {"c", "a", "b"}  # the finished step is no longer busy


# --- the endpoints ----------------------------------------------------------------------------------


class FakeMapping:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail: Exception | None = None
        self.rows = [
            {"chunk_id": "c1", "section": "A", "asset_id": "id:1", "asset": "x.a", "status": "proposed", "confidence": "high", "relation": "defines", "evidence": "e", "reason": "r", "category": "", "note": ""},
            {"chunk_id": "c1", "section": "A", "asset_id": "id:2", "asset": "x.b", "status": "accepted", "confidence": "medium", "relation": "defines", "evidence": "e", "reason": "r", "category": "", "note": ""},
        ]

    def check(self, settings, client):  # noqa: ANN001
        self.calls.append(("check", None))
        return {"catalogue": {"tables": 12, "columns": 126}, "chunks": {"total": 91}}

    def execute(self, settings, opts, client, progress=None, models=None, writer_factory=None, **kw):  # noqa: ANN001
        self.calls.append(("execute", opts))
        if self.fail:
            raise self.fail
        if progress:
            from app.mapping.engine import Chunk, ChunkResult

            for i in (1, 2):
                progress(i, 2, ChunkResult(Chunk(f"c{i}", "d", "T", f"1 > {i}", "h", "text")))
        return {"run_id": "RUN-123456", "summary": {"to_create": 3}, "report_path": "r.json", "written": {"created": 3, "superseded": 0} if opts.write else None, "failed_chunks": [], "dry_run": not opts.write}

    def list_reports(self, client, report_dir=None, limit=30):  # noqa: ANN001
        return [{"run_id": "RUN-123456", "created": 1.0, "mode": "append", "chunks": 4, "links": 3, "written": False}]

    def report_for_review(self, client, run_id, report_dir=None):  # noqa: ANN001
        if run_id == "MISSING-0001":
            raise mapping_service.ReportNotFound("No saved report for run MISSING-0001.")
        return {"run_id": run_id, "written": False, "summary": {"to_create": 3}, "links": self.rows}

    def apply_report(self, settings, client, run_id, accept, reject, writer_factory=None, report_dir=None):  # noqa: ANN001
        self.calls.append(("apply", (run_id, accept, reject)))
        if run_id == "WRITTEN-0001":
            raise mapping_service.AlreadyWritten("That run was already written to Neo4j.")
        if run_id == "MISSING-0001":
            raise mapping_service.ReportNotFound("No saved report for run MISSING-0001.")
        if not accept and not reject:
            raise ValueError("Pick at least one link to write.")
        return {"run_id": run_id, "created": len(accept) + len(reject), "accepted": len(accept), "rejected": len(reject), "skipped": []}

    def recent_runs(self, client, limit=20):  # noqa: ANN001
        return [{"run_id": "RUN-123456", "mode": "append", "links_by_status": {"proposed": 2}}]

    def links_of_run(self, client, run_id):  # noqa: ANN001
        self.calls.append(("links", run_id))
        return self.rows


class FakeWriter:
    log: list[tuple[str, Any]] = []

    def __init__(self, settings: Settings):
        self.closed = False

    def decide(self, run_id, pairs, status):  # noqa: ANN001
        FakeWriter.log.append(("decide", (run_id, pairs, status)))
        return len(pairs)

    def review(self, run_id, status, min_confidence):  # noqa: ANN001
        FakeWriter.log.append(("review", (run_id, status, min_confidence)))
        return 4

    def restore(self, run_id):  # noqa: ANN001
        FakeWriter.log.append(("restore", run_id))
        return {"retracted": 3, "restored": 2}

    def close(self) -> None:
        self.closed = True


class FakeNeo4j:
    def close(self) -> None:  # the app closes its client on shutdown
        pass


def make_app(fake: FakeFabric | None = None, settings: Settings | None = None, live: bool = True, mapping: FakeMapping | None = None):  # noqa: ANN201
    s = settings or cfg(mode="live" if live else "snapshot")
    fake = fake or FakeFabric(items=ITEMS)
    deps = JobsDeps(hub=FabricHub(s, fake.client(s)), store=JobStore(synchronous=True), mapping=mapping or FakeMapping(), writer_factory=FakeWriter, models_factory=lambda st: object(), sleep=lambda x: None)
    FakeWriter.log = []
    app = create_app(s, repo=SnapshotRepository([], []), client=FakeNeo4j() if live else None, jobs=deps)
    return TestClient(app), deps, fake


def test_config_lists_everything_the_page_needs() -> None:
    c, _, _ = make_app()
    body = c.get("/api/jobs/config").json()
    assert {j["key"] for j in body["jobs"]} == {j.key for j in JOBS} and len(body["chains"]) == len(CHAINS)
    assert list(body["groups"])[:4] == ["source", "lineage", "policies", "llm"]  # the order of the flow the page draws
    assert body["groups"]["source"] == "Structured data harvesting" and body["groups"]["policies"] == "Unstructured data chunking"
    assert all(body["group_notes"][g] for g in ("source", "lineage", "policies", "llm"))
    assert "meet" not in json.dumps(body).lower()  # nobody's name on the page
    assert body["fabric"] == {"configured": True, "auth_mode": "token", "workspace": WS, "param_style": "array"}
    assert body["mapping"] == {"write_enabled": True, "live": True} and "policies" in body["groups"]
    llm = next(j for j in body["jobs"] if j["key"] == "llm_mapping_nb")
    assert {p["name"] for p in llm["params"]} >= {"MODE", "WRITE", "LIMIT"} and next(p for p in llm["params"] if p["name"] == "MODE")["options"] == ["append", "overwrite"]
    assert "secret" not in json.dumps(body).lower() and c.get("/api/jobs/config").json()["mapping"]["live"] is True


def test_status_reports_what_exists_in_the_workspace() -> None:
    c, _, _ = make_app()
    body = c.get("/api/jobs/fabric/status").json()
    assert body["configured"] and body["error"] is None and body["problems"] == [] and 3500 < body["token_expires_in"] <= 3600
    assert body["items"]["policy_chunk"] == {"found": True, "item_id": "id-policy_docs_chunk_step2", "display_name": "policy_docs_chunk_step2", "looked_for": ["02_policy_docs_chunk_and_map_step2", "02_policy_docs_chunk_and_map_step2(1)", "policy_docs_chunk_step2", "policy_docs_chunk_and_map_step2"]}
    assert body["items"]["b02"]["found"] is False and body["items"]["pl_accounts"]["found"] is True  # Set B is not imported yet


def test_status_without_credentials_explains_what_to_set_and_calls_nothing() -> None:
    fake = FakeFabric(items=ITEMS)
    c, _, _ = make_app(fake, cfg(fabric_access_token="", fabric_workspace_id="", fabric_workspace_name=""))
    body = c.get("/api/jobs/fabric/status").json()
    assert body["configured"] is False and len(body["problems"]) == 2 and body["items"] == {} and fake.requests == []
    assert "FABRIC_TENANT_ID" in body["problems"][0] and "FABRIC_WORKSPACE" in body["problems"][1]


def test_status_shows_a_fabric_failure_instead_of_failing_the_page() -> None:
    fake = FakeFabric(items=ITEMS)
    fake.fail_with = 403
    c, _, _ = make_app(fake)
    r = c.get("/api/jobs/fabric/status")
    assert r.status_code == 200 and "Contributor" in r.json()["error"] and r.json()["items"] == {}


def test_run_a_job_end_to_end() -> None:
    c, deps, fake = make_app(FakeFabric(items=ITEMS, scripts={"policy_docs_chunk_step2": ["InProgress", "Completed"]}))
    r = c.post("/api/jobs/fabric/run", json={"job": "policy_chunk"})
    assert r.status_code == 200
    run = c.get(f"/api/jobs/runs/{r.json()['id']}").json()
    assert run["status"] == "succeeded" and run["kind"] == "fabric" and run["steps"][0]["status"] == "Completed" and run["steps"][0]["instance_id"]
    assert [x["id"] for x in c.get("/api/jobs/runs").json()] == [run["id"]]


def test_run_refuses_what_it_should() -> None:
    c, _, fake = make_app()
    assert c.post("/api/jobs/fabric/run", json={"job": "nope"}).status_code == 404
    risky = c.post("/api/jobs/fabric/run", json={"job": "pl_accounts"})
    assert risky.status_code == 400 and "reloads dim.account" in risky.json()["error"]["message"]
    assert c.post("/api/jobs/fabric/run", json={"job": "pl_accounts", "confirm": True}).status_code == 200
    assert c.post("/api/jobs/fabric/run", json={"job": "policy_chunk", "parameters": {"MODE": "x"}}).status_code == 400
    assert c.post("/api/jobs/fabric/run", json={"job": "llm_mapping_nb", "parameters": {"MODE": "overwrite", "WRITE": True}}).status_code == 400  # unconfirmed overwrite
    assert len([p for p in fake.posted if "item_id" in p]) == 1  # only the confirmed one started


def test_run_without_credentials_is_a_503_with_instructions() -> None:
    c, _, _ = make_app(settings=cfg(fabric_access_token="", mode="live"))
    r = c.post("/api/jobs/fabric/run", json={"job": "policy_chunk"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "LineageStudio.FabricNotConfigured" and "FABRIC_ACCESS_TOKEN" in r.json()["error"]["message"]


def test_a_fabric_error_is_a_502_with_a_safe_message() -> None:
    fake = FakeFabric(items=ITEMS)
    fake.fail_with = 401
    c, _, _ = make_app(fake)
    run = c.post("/api/jobs/fabric/run", json={"job": "policy_chunk"}).json()
    assert run["status"] == "failed" and "sign-in" in run["error"] and "Bearer" not in json.dumps(run)  # surfaced on the run


def test_a_job_cannot_be_started_twice_at_once() -> None:
    c, deps, _ = make_app()
    busy = deps.store.new("fabric", "policy_chunk", "x", [StepState("policy_chunk", "x", "InProgress")])
    busy.status = "running"
    r = c.post("/api/jobs/fabric/run", json={"job": "policy_chunk"})
    assert r.status_code == 409 and "policy_chunk" in r.json()["error"]["message"]
    chain = c.post("/api/jobs/fabric/chain", json={"chain": "llm_policy_changed"})  # contains policy_chunk
    assert chain.status_code == 409


def test_chain_endpoint_runs_in_order_with_overrides() -> None:
    c, deps, fake = make_app()
    r = c.post("/api/jobs/fabric/chain", json={"chain": "llm_policy_changed", "overrides": {"llm_mapping_nb": {"WRITE": False, "LIMIT": 3}}})
    assert r.status_code == 200 and r.json()["status"] == "succeeded"
    assert [p["item_id"] for p in fake.posted] == ["id-policy_docs_chunk_step2", "id-policy_chunks_to_neo4j_vector_step3", "id-nb_llm_policy_mapping"]
    assert fake.posted[2]["body"]["parameters"] == [{"name": "WRITE", "value": False, "type": "Boolean"}, {"name": "LIMIT", "value": 3, "type": "Number"}]
    assert fake.posted[0]["body"] is None


def test_chain_endpoint_validation_and_confirmation() -> None:
    c, _, fake = make_app()
    assert c.post("/api/jobs/fabric/chain", json={"chain": "nope"}).status_code == 404
    assert c.post("/api/jobs/fabric/chain", json={"chain": "a_demo", "overrides": {"pl_accounts": {}}}).status_code == 400
    unconfirmed = c.post("/api/jobs/fabric/chain", json={"chain": "structured_all"})  # reloads lakehouse tables
    assert unconfirmed.status_code == 400 and "reloads" in unconfirmed.json()["error"]["message"] and fake.posted == []
    assert c.post("/api/jobs/fabric/chain", json={"chain": "structured_all", "confirm": True}).status_code == 200
    assert c.post("/api/jobs/fabric/chain", json={"chain": "a_demo"}).status_code == 200  # nothing risky in it, no confirmation


def test_switching_writes_off_blocks_every_writing_button() -> None:
    c, _, fake = make_app(settings=cfg(mode="live", mapping_ui_write=False))
    assert c.get("/api/jobs/config").json()["mapping"]["write_enabled"] is False
    for url, body in (
        ("/api/jobs/mapping/run", {"write": True}),
        ("/api/jobs/mapping/decide", {"run_id": "RUN-123456", "status": "accepted", "decisions": [{"chunk_id": "c", "asset_id": "a"}]}),
        ("/api/jobs/mapping/review", {"run_id": "RUN-123456", "status": "accepted"}),
        ("/api/jobs/mapping/restore", {"run_id": "RUN-123456"}),
        ("/api/jobs/mapping/apply", {"run_id": "RUN-123456", "accept": [{"chunk_id": "c", "asset_id": "a"}]}),
        ("/api/jobs/fabric/run", {"job": "llm_mapping_nb", "parameters": {"WRITE": True}}),
    ):
        r = c.post(url, json=body)
        assert r.status_code == 403 and "MAPPING_UI_WRITE" in r.json()["error"]["message"], url
    assert FakeWriter.log == [] and fake.posted == []
    assert c.post("/api/jobs/mapping/run", json={}).status_code == 200  # a dry run is still allowed


def test_cancel_endpoint() -> None:
    c, deps, _ = make_app()
    assert c.post("/api/jobs/runs/nope/cancel").status_code == 404
    mapping = deps.store.new("mapping", "mapping", "m")
    mapping.status = "running"
    assert c.post(f"/api/jobs/runs/{mapping.id}/cancel").status_code == 400
    done = deps.store.new("fabric", "x", "x")
    done.status = "succeeded"
    assert c.post(f"/api/jobs/runs/{done.id}/cancel").status_code == 409
    live = deps.store.new("fabric", "y", "y")
    live.status = "running"
    assert c.post(f"/api/jobs/runs/{live.id}/cancel").json()["cancel_requested"] is True and live.cancel_requested
    assert c.get("/api/jobs/runs/nope").status_code == 404


# --- the mapping endpoints --------------------------------------------------------------------------


def test_mapping_check_and_history_pass_through() -> None:
    c, deps, _ = make_app()
    assert c.post("/api/jobs/mapping/check").json()["catalogue"]["columns"] == 126
    assert c.get("/api/jobs/mapping/runs").json()[0]["run_id"] == "RUN-123456"


def test_mapping_needs_live_neo4j() -> None:
    c, _, _ = make_app(live=False)
    for r in (c.post("/api/jobs/mapping/check"), c.post("/api/jobs/mapping/run", json={}), c.get("/api/jobs/mapping/runs")):
        assert r.status_code == 503 and "live Neo4j" in r.json()["error"]["message"]
    assert c.get("/api/jobs/config").json()["mapping"]["live"] is False


def test_mapping_dry_run_records_progress_and_result() -> None:
    m = FakeMapping()
    c, deps, _ = make_app(mapping=m)
    run = c.post("/api/jobs/mapping/run", json={"mode": "append", "docs": ["Customer"], "limit": 5, "workers": 2}).json()
    assert run["status"] == "succeeded" and run["progress"] == {"done": 2, "total": 2} and run["result"]["dry_run"] is True
    assert run["result"]["run_id"] == "RUN-123456" and len(run["log"]) == 2 and run["log"][0].startswith("[1/2]")
    opts = next(o for name, o in m.calls if name == "execute")
    assert (opts.mode, opts.write, opts.docs, opts.limit, opts.workers) == ("append", False, ["Customer"], 5, 2)


def test_mapping_write_and_overwrite_rules() -> None:
    m = FakeMapping()
    c, _, _ = make_app(mapping=m)
    ok = c.post("/api/jobs/mapping/run", json={"write": True}).json()
    assert ok["status"] == "succeeded" and ok["result"]["written"] == {"created": 3, "superseded": 0}
    unconfirmed = c.post("/api/jobs/mapping/run", json={"write": True, "mode": "overwrite"})
    assert unconfirmed.status_code == 400 and "Confirm" in unconfirmed.json()["error"]["message"]
    assert c.post("/api/jobs/mapping/run", json={"write": True, "mode": "overwrite", "confirm_overwrite": True}).json()["status"] == "succeeded"
    assert c.post("/api/jobs/mapping/run", json={"mode": "merge"}).status_code == 422
    assert c.post("/api/jobs/mapping/run", json={"workers": 99}).status_code == 422


def test_mapping_failures_are_shown_on_the_run() -> None:
    m = FakeMapping()
    m.fail = LLMNotConfigured("Missing in .env: AZURE_OPEN_AI_KEY")
    c, _, _ = make_app(mapping=m)
    run = c.post("/api/jobs/mapping/run", json={}).json()
    assert run["status"] == "failed" and "AZURE_OPEN_AI_KEY" in run["error"]


def test_only_one_mapping_run_at_a_time() -> None:
    c, deps, _ = make_app()
    busy = deps.store.new("mapping", "mapping", "m")
    busy.status = "running"
    r = c.post("/api/jobs/mapping/run", json={})
    assert r.status_code == 409 and "mapping" in r.json()["error"]["message"]


def test_review_links_are_listed_and_filtered() -> None:
    m = FakeMapping()
    c, _, _ = make_app(mapping=m)
    assert [r["asset"] for r in c.get("/api/jobs/mapping/links", params={"run_id": "RUN-123456"}).json()] == ["x.a", "x.b"]
    only = c.get("/api/jobs/mapping/links", params={"run_id": "RUN-123456", "status": "proposed"}).json()
    assert [r["asset_id"] for r in only] == ["id:1"]
    assert c.get("/api/jobs/mapping/links", params={"run_id": "bad id!"}).status_code == 400


def test_accept_reject_and_undo_call_the_writer() -> None:
    c, _, _ = make_app()
    assert c.post("/api/jobs/mapping/decide", json={"run_id": "RUN-123456", "status": "rejected", "decisions": [{"chunk_id": "c1", "asset_id": "id:1"}, {"chunk_id": "c2", "asset_id": "id:9"}]}).json() == {"changed": 2}
    assert c.post("/api/jobs/mapping/review", json={"run_id": "RUN-123456", "status": "accepted", "min_confidence": "high"}).json() == {"changed": 4}
    assert c.post("/api/jobs/mapping/restore", json={"run_id": "RUN-123456"}).json() == {"retracted": 3, "restored": 2}
    assert FakeWriter.log == [
        ("decide", ("RUN-123456", [("c1", "id:1"), ("c2", "id:9")], "rejected")),
        ("review", ("RUN-123456", "accepted", "high")),
        ("restore", "RUN-123456"),
    ]


def test_write_endpoints_validate_their_input() -> None:
    c, _, _ = make_app()
    assert c.post("/api/jobs/mapping/decide", json={"run_id": "RUN-123456", "status": "deleted", "decisions": [{"chunk_id": "c", "asset_id": "a"}]}).status_code == 422
    assert c.post("/api/jobs/mapping/decide", json={"run_id": "RUN-123456", "status": "accepted", "decisions": []}).status_code == 422
    assert c.post("/api/jobs/mapping/restore", json={"run_id": "x; DROP"}).status_code == 422
    assert FakeWriter.log == []


# --- the real workspace ------------------------------------------------------------------------------

# What Fabric actually returned for the Ontology_Lineage workspace (read through the API, 23 Nov 2026).
REAL_NOTEBOOKS = [
    "01_nb_lineage_extractor_structure_data_step1", "02_load_lineage_graph", "02_policy_docs_chunk_and_map_step2(1)",
    "02_policy_docs_chunk_and_map_step2_OLD", "03_policy_chunks_to_neo4j_vector_step3",
    "04_embed_lineage_nodes_to_neo4j_step4", "05_policy_map_to_graph_step5(1)", "05_policy_map_to_graph_step5_OLD",
    "NB_Add_Risk_Bucket_Flag", "nb_lineage_extractor_1", "ontology_lineage_extract_metadata_and_chunks(1)",
    "policy_chunks_to_neo4j_vector(1)", "policy_docx_chunking_to_lakehouse", "policy_to_existing_columns_link",
    "policy_to_metadata_mapping",
]
REAL_PIPELINES = ["PL_CP_Accounts", "PL_CP_Branches", "PL_CP_Customers_Masked", "PL_CP_Loan_Portfolio", "Pl_CP_Txn_Monthly_Summary"]
# Notebooks that exist in the workspace but must never be started from a button.
NEVER_TRIGGER = {
    "02_load_lineage_graph",  # the old loader: inserts synthetic Entity nodes
    "policy_to_metadata_mapping", "policy_to_existing_columns_link", "ontology_lineage_extract_metadata_and_chunks(1)",
    "policy_docx_chunking_to_lakehouse", "policy_chunks_to_neo4j_vector(1)", "NB_Add_Risk_Bucket_Flag",  # superseded or run by a pipeline
    "nb_lineage_extractor_1",  # the other extractor candidate; Meet's is ...structure_data_step1 (confirmed)
    "02_policy_docs_chunk_and_map_step2_OLD", "05_policy_map_to_graph_step5_OLD",  # the renamed originals (old register mapping)
}


def test_the_exact_name_beats_the_import_copy_once_it_is_renamed() -> None:
    names = ["02_policy_docs_chunk_and_map_step2", "02_policy_docs_chunk_and_map_step2(1)", "02_policy_docs_chunk_and_map_step2_OLD"]
    resolver = ItemResolver(FakeFabric(items={"Notebook": names, "DataPipeline": []}).client(cfg()))
    assert resolver.resolve(JOBS_BY_KEY["policy_chunk"])["displayName"] == "02_policy_docs_chunk_and_map_step2"


def test_the_real_workspace_names_resolve_to_the_right_jobs() -> None:
    fake = FakeFabric(items={"Notebook": REAL_NOTEBOOKS, "DataPipeline": REAL_PIPELINES})
    resolver = ItemResolver(fake.client(cfg()))
    found = {j.key: (item["displayName"] if (item := resolver.resolve(j)) else None) for j in JOBS}
    # the edited notebooks were imported as "(1)" copies and the old ones renamed "_OLD": only the copies match
    assert found["policy_chunk"] == "02_policy_docs_chunk_and_map_step2(1)"
    assert found["policy_load_graph"] == "03_policy_chunks_to_neo4j_vector_step3"
    assert found["lineage_embed"] == "04_embed_lineage_nodes_to_neo4j_step4"
    assert found["policy_map_legacy"] == "05_policy_map_to_graph_step5(1)"
    assert [found[k] for k in ("pl_accounts", "pl_branches", "pl_customers", "pl_loans", "pl_txn")] == REAL_PIPELINES
    assert found["lineage_extract"] == "01_nb_lineage_extractor_structure_data_step1"  # Meet's, confirmed
    # not in the workspace yet: left unmatched rather than guessed
    assert found["llm_mapping_nb"] is None
    assert all(found[k] is None for k in ("b01", "b02", "b03", "b04", "b05", "b06"))
    assert not (set(found.values()) & NEVER_TRIGGER)


def test_no_job_can_ever_match_a_notebook_that_must_not_run() -> None:
    listed = {n for j in JOBS for n in j.names}
    assert not (listed & NEVER_TRIGGER), listed & NEVER_TRIGGER


# --- review a dry run before writing ------------------------------------------------------------------


def test_saved_reports_are_listed_and_readable() -> None:
    c, _, _ = make_app()
    assert c.get("/api/jobs/mapping/reports").json()[0] == {"run_id": "RUN-123456", "created": 1.0, "mode": "append", "chunks": 4, "links": 3, "written": False}
    body = c.get("/api/jobs/mapping/report", params={"run_id": "RUN-123456"}).json()
    assert body["written"] is False and [l["asset"] for l in body["links"]] == ["x.a", "x.b"]
    assert c.get("/api/jobs/mapping/report", params={"run_id": "MISSING-0001"}).status_code == 404
    assert c.get("/api/jobs/mapping/report", params={"run_id": "../../etc"}).status_code == 400


def test_reports_need_live_neo4j_to_know_what_is_written() -> None:
    c, _, _ = make_app(live=False)
    assert c.get("/api/jobs/mapping/reports").status_code == 503


def test_apply_writes_only_what_was_picked() -> None:
    m = FakeMapping()
    c, _, _ = make_app(mapping=m)
    r = c.post("/api/jobs/mapping/apply", json={"run_id": "RUN-123456", "accept": [{"chunk_id": "c1", "asset_id": "id:1"}], "reject": [{"chunk_id": "c1", "asset_id": "id:2"}]})
    assert r.status_code == 200 and r.json() == {"run_id": "RUN-123456", "created": 2, "accepted": 1, "rejected": 1, "skipped": []}
    assert next(a for n, a in m.calls if n == "apply") == ("RUN-123456", [("c1", "id:1")], [("c1", "id:2")])


def test_apply_errors_map_to_clear_statuses() -> None:
    c, _, _ = make_app()
    pick = {"accept": [{"chunk_id": "c1", "asset_id": "id:1"}]}
    already = c.post("/api/jobs/mapping/apply", json={"run_id": "WRITTEN-0001", **pick})
    assert already.status_code == 409 and "already written" in already.json()["error"]["message"]
    assert c.post("/api/jobs/mapping/apply", json={"run_id": "MISSING-0001", **pick}).status_code == 404
    empty = c.post("/api/jobs/mapping/apply", json={"run_id": "RUN-123456"})
    assert empty.status_code == 400 and "at least one" in empty.json()["error"]["message"]
    assert c.post("/api/jobs/mapping/apply", json={"run_id": "x; DROP", **pick}).status_code == 422
    assert c.post("/api/jobs/mapping/apply", json={"run_id": "RUN-123456", "accept": [{"chunk_id": "", "asset_id": "a"}]}).status_code == 422
