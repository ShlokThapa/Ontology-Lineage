"""Reviewing a saved dry run before anything is written: listing reports, and writing only the picked links.

Neo4j and the writer are fakes; the report is a real JSON file in a temp folder.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.mapping.service import AlreadyWritten, ReportNotFound, apply_report, list_reports, load_report, report_for_review

RUN = "20261123T142654-f06796"
MODELS = {"mapper": "gpt-x", "verifier": "gpt-x", "adjudicator": "gpt-x"}


def row(chunk: str, asset: str, status: str = "proposed", confidence: str = "high", will_write: bool = True, **kw: Any) -> dict[str, Any]:
    return {
        "run_id": RUN, "chunk_id": chunk, "doc_id": "pol-1", "doc_title": "Credit Risk Policy", "section_path": f"1 > {chunk}",
        "asset": asset, "asset_id": f"id:{asset}", "relation": "defines", "confidence": confidence, "status": status,
        "basis": "direct", "category": None, "evidence": f"quote about {asset}", "reason": "because",
        "votes": json.dumps({"mapper": "supported", "verifier": "supported"}), "note": "", "will_write": will_write, **kw,
    }


ROWS = [
    row("c1", "a.one"), row("c1", "a.two", confidence="medium"), row("c2", "b.three"),
    row("c2", "b.dropped", status="dropped"),  # the models rejected it
    row("c3", "c.skipped", will_write=False),  # already mapped when the dry run was planned
]


def save(folder: Path, run_id: str = RUN, rows: list[dict] | None = None, age: float = 0) -> Path:
    data = {"summary": {"run_id": run_id, "mode": "append", "chunks_processed": 3, "to_create": 3, "skipped_existing": 1,
                        "links_rejected_by_code": 0, "independent_verification": False, "models": MODELS,
                        "catalogue": {"hash": "cafe1234"}},
            "links": ROWS if rows is None else rows}
    p = folder / f"mapping-{run_id}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    if age:
        os.utime(p, (time.time() - age, time.time() - age))
    return p


class Res:
    def __init__(self, records: list[dict]):
        self.records = records


class FakeNeo4j:
    def __init__(self, written: set[str] | None = None, existing: dict[tuple[str, str], list[str]] | None = None):
        self.written, self.existing = written or set(), existing or {}

    def run_read(self, query: str, params=None, max_rows=None):  # noqa: ANN001
        if "MappingRun" in query:
            return Res([{"run_id": r} for r in self.written])
        return Res([{"chunk_id": c, "asset_id": a, "status": s} for (c, a), sts in self.existing.items() for s in sts])


class FakeWriter:
    instances: list["FakeWriter"] = []

    def __init__(self, settings: Settings):
        self.applied: list[tuple[Any, ...]] = []
        self.closed = False
        FakeWriter.instances.append(self)

    def apply(self, plan, models, doc_ids, catalogue_hash=""):  # noqa: ANN001
        self.applied.append((plan, models, doc_ids, catalogue_hash))
        return {"created": len(plan.create), "superseded": 0}

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def folder(tmp_path: Path) -> Path:
    FakeWriter.instances = []
    save(tmp_path)
    return tmp_path


S = Settings(mode="snapshot")


def apply(folder: Path, accept=(), reject=(), client=None):  # noqa: ANN001, ANN201
    return apply_report(S, client or FakeNeo4j(), RUN, list(accept), list(reject), writer_factory=FakeWriter, report_dir=folder)


# --- listing and reading reports --------------------------------------------------------------------


def test_reports_are_listed_newest_first_and_marked_written_or_not(tmp_path: Path) -> None:
    save(tmp_path, "OLDRUN-0001", age=500)
    save(tmp_path, RUN, age=100)
    (tmp_path / "mapping-BROKEN-0001.json").write_text("{not json", encoding="utf-8")  # ignored, not fatal
    got = list_reports(FakeNeo4j(written={"OLDRUN-0001"}), tmp_path)
    assert [r["run_id"] for r in got] == [RUN, "OLDRUN-0001"]
    assert got[0]["written"] is False and got[1]["written"] is True
    assert got[0]["links"] == 3 and got[0]["chunks"] == 3 and got[0]["mode"] == "append"  # dropped and already-mapped excluded


def test_report_for_review_shows_only_links_a_person_can_still_decide(folder: Path) -> None:
    r = report_for_review(FakeNeo4j(), RUN, folder)
    assert [(x["asset"], x["status"]) for x in r["links"]] == [("a.one", "proposed"), ("a.two", "proposed"), ("b.three", "proposed")]
    assert r["written"] is False and r["summary"]["to_create"] == 3
    assert "votes" not in r["links"][0] and "will_write" not in r["links"][0]  # only what the screen needs
    assert report_for_review(FakeNeo4j(written={RUN}), RUN, folder)["written"] is True


@pytest.mark.parametrize("bad", ["../secrets", "a/b", "x", "run id", "", "..\\..\\x", "a" * 80])
def test_run_ids_cannot_point_outside_the_reports_folder(folder: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        load_report(bad, folder)


def test_a_missing_report_is_reported_as_not_found(folder: Path) -> None:
    with pytest.raises(ReportNotFound, match="No saved report"):
        load_report("MISSING-0001", folder)


# --- writing only what was picked -------------------------------------------------------------------


def test_only_the_picked_links_are_written_with_the_status_the_person_chose(folder: Path) -> None:
    out = apply(folder, accept=[("c1", "id:a.one"), ("c2", "id:b.three")], reject=[("c1", "id:a.two")])
    assert (out["created"], out["accepted"], out["rejected"], out["skipped"]) == (3, 2, 1, [])
    ((plan, models, doc_ids, cat_hash),) = FakeWriter.instances[0].applied
    assert plan.run_id == RUN and plan.mode == "append" and plan.supersede == []  # append only: never retires anything
    assert {(ln.chunk_id, ln.asset_id): ln.status for ln in plan.create} == {
        ("c1", "id:a.one"): "accepted", ("c2", "id:b.three"): "accepted", ("c1", "id:a.two"): "rejected"}
    assert models == MODELS and doc_ids == {"c1": "pol-1", "c2": "pol-1"} and cat_hash == "cafe1234"
    first = next(ln for ln in plan.create if ln.asset_id == "id:a.one")
    assert first.evidence == "quote about a.one" and first.confidence == "high" and "reviewed before writing" in first.note
    assert first.votes == {"mapper": "supported", "verifier": "supported"}
    assert FakeWriter.instances[0].closed


def test_links_come_from_the_saved_report_not_from_the_caller(folder: Path) -> None:
    with pytest.raises(ValueError, match="not in that report"):
        apply(folder, accept=[("c1", "id:invented")])  # a pair the report never proposed
    with pytest.raises(ValueError, match="not in that report"):
        apply(folder, accept=[("c2", "id:b.dropped")])  # the models dropped it: it cannot be forced in
    with pytest.raises(ValueError, match="not in that report"):
        apply(folder, accept=[("c3", "id:c.skipped")])  # was already mapped when planned
    assert FakeWriter.instances == []  # nothing was even opened


def test_input_is_validated_before_anything_is_written(folder: Path) -> None:
    with pytest.raises(ValueError, match="both accepted and rejected"):
        apply(folder, accept=[("c1", "id:a.one")], reject=[("c1", "id:a.one")])
    with pytest.raises(ValueError, match="at least one"):
        apply(folder)
    assert FakeWriter.instances == []


def test_a_run_cannot_be_written_twice(folder: Path) -> None:
    with pytest.raises(AlreadyWritten, match="already written"):
        apply(folder, accept=[("c1", "id:a.one")], client=FakeNeo4j(written={RUN}))
    assert FakeWriter.instances == []


def test_the_graph_is_rechecked_at_write_time(folder: Path) -> None:
    client = FakeNeo4j(existing={("c1", "id:a.one"): ["accepted"], ("c1", "id:a.two"): ["rejected"], ("c2", "id:b.three"): ["superseded"]})
    out = apply(folder, accept=[("c1", "id:a.one"), ("c1", "id:a.two"), ("c2", "id:b.three")], client=client)
    assert {(s["ref"], s["reason"]) for s in out["skipped"]} == {("a.one", "already_mapped"), ("a.two", "previously_rejected")}
    assert out["created"] == 1  # a superseded pair may come back
    assert [ln.asset_id for ln in FakeWriter.instances[0].applied[0][0].create] == ["id:b.three"]


def test_nothing_is_written_when_every_pick_is_skipped(folder: Path) -> None:
    client = FakeNeo4j(existing={("c1", "id:a.one"): ["proposed"]})
    out = apply(folder, accept=[("c1", "id:a.one")], client=client)
    assert out["created"] == 0 and len(out["skipped"]) == 1 and FakeWriter.instances == []


def test_a_rejected_pick_is_kept_as_a_record_so_it_never_returns(folder: Path) -> None:
    out = apply(folder, reject=[("c1", "id:a.two"), ("c2", "id:b.three")])
    assert (out["accepted"], out["rejected"]) == (0, 2)
    assert {ln.status for ln in FakeWriter.instances[0].applied[0][0].create} == {"rejected"}


def test_category_links_keep_their_category(tmp_path: Path) -> None:
    FakeWriter.instances = []
    save(tmp_path, rows=[row("c1", "a.one", basis="category", category="@PII")])
    apply_report(S, FakeNeo4j(), RUN, [("c1", "id:a.one")], [], writer_factory=FakeWriter, report_dir=tmp_path)
    ln = FakeWriter.instances[0].applied[0][0].create[0]
    assert ln.basis == "category" and ln.category == "@PII"
