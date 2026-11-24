"""Command line for the policy-mapping job. The same functions back the Jobs page in the app.

    python -m app.mapping check                       what would run: models, catalogue, chunks, existing links
    python -m app.mapping run                         dry run: calls the models, writes only a report file
    python -m app.mapping run --write                 append mode: add links that are missing, touch nothing else
    python -m app.mapping run --mode overwrite --write --confirm-overwrite
                                                      replace the live links of the chosen chunks (old ones are
                                                      kept as `superseded`; undo with `restore`)
    python -m app.mapping review --run ID --accept    accept (or --reject) a run's proposed links
    python -m app.mapping restore --run ID            undo a run
"""

from __future__ import annotations

import argparse
import json
import sys

from ..chat.llm import LLMNotConfigured
from ..config import get_settings
from ..neo4j_client import DatabaseUnavailable, Neo4jClient
from . import service
from .models import load_models
from .service import RunOptions
from .store import MappingWriter


def cmd_check(args: argparse.Namespace) -> int:
    settings = get_settings()
    client = Neo4jClient(settings)
    try:
        info = service.check(settings, client)
        cat, ch = info["catalogue"], info["chunks"]
        print(f"catalogue : {cat['tables']} tables/views, {cat['columns']} columns, categories {cat['categories']} (hash {cat['hash']})")
        print(f"chunks    : {ch['total']} in {ch['documents']} documents; {ch['with_live_link']} already have a live link, {ch['without_link']} have none")
        print(f"existing  : {info['existing_links']} links in the graph")
        for m in info["models"]:
            label = m["model"] or ("(not set: model 1 does this too)" if m["slot"] > 1 else "(NOT CONFIGURED)")
            print(f"model {m['slot']}   : {m['role']:11} {label}")
        if not info["independent_verifier"]:
            print("note      : model 1 verifies its own links. That is not independent, so links stay 'proposed' for a person;")
            print("            pass --accept-self-verified to let high-confidence ones through, or add model 2 (e.g. if token-limited).")
        return 0
    finally:
        client.close()


def cmd_run(args: argparse.Namespace) -> int:
    if args.mode == "overwrite" and args.write and not args.confirm_overwrite:
        print("Overwrite replaces the live links of the chosen chunks. Add --confirm-overwrite to proceed "
              "(the old links are kept as 'superseded' and `restore` undoes it).", file=sys.stderr)
        return 2
    settings = get_settings()
    client = Neo4jClient(settings)
    try:
        opts = RunOptions(
            mode=args.mode, write=args.write, confirm_overwrite=args.confirm_overwrite, only_unlinked=args.only_unlinked,
            no_auto_accept=args.no_auto_accept, accept_self_verified=args.accept_self_verified,
            docs=args.docs or [], chunks=args.chunks or [], limit=args.limit, workers=args.workers, use_cache=not args.no_cache,
        )
        models = load_models(settings)
        print(f"{args.mode.upper()} | models {models.names()} | {'WRITE' if args.write else 'dry run'}")
        if not models.independent_verifier:
            trust = "high-confidence links will be accepted" if args.accept_self_verified else "links stay 'proposed'"
            print(f"verifier is the same model as the mapper (not independent): {trust}")

        def progress(done, total, res):  # type: ignore[no-untyped-def]
            live = len([ln for ln in res.links if ln.status != "dropped"])
            flag = "ERROR " + (res.error or "") if res.error else f"{live} links" + (" (cached)" if res.cached else "")
            print(f"  [{done}/{total}] {res.chunk.section_path[:60]:60} {flag}")

        out = service.execute(settings, opts, client, progress, models=models)
        print("\n" + json.dumps(out["summary"], indent=2))
        print(f"\nreport: {out['report_path']}")
        if out["dry_run"]:
            print("Dry run: nothing was written to the graph. Add --write to apply this plan.")
            return 0
        if out["failed_chunks"] and args.mode == "overwrite":
            print(f"{len(out['failed_chunks'])} chunk(s) failed; their existing links are left untouched.")
        print(f"WRITTEN: {out['written']}")
        print(f"Undo with: python -m app.mapping restore --run {out['run_id']}")
        return 0
    finally:
        client.close()


def cmd_restore(args: argparse.Namespace) -> int:
    writer = MappingWriter(get_settings())
    try:
        print(writer.restore(args.run))
    finally:
        writer.close()
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    writer = MappingWriter(get_settings())
    try:
        n = writer.review(args.run, "accepted" if args.accept else "rejected", args.min_confidence)
        print(f"{n} proposed link(s) {'accepted' if args.accept else 'rejected'}")
    finally:
        writer.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.mapping", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="show what a run would use, without calling any model").set_defaults(fn=cmd_check)

    r = sub.add_parser("run", help="map policy chunks to data assets")
    r.add_argument("--mode", choices=["append", "overwrite"], default="append")
    r.add_argument("--write", action="store_true", help="apply the plan (default is a dry run)")
    r.add_argument("--confirm-overwrite", action="store_true", help="required to write in overwrite mode")
    r.add_argument("--only-unlinked", action="store_true", help="skip chunks that already have a live link")
    r.add_argument("--no-auto-accept", action="store_true", help="make every new link 'proposed', none 'accepted'")
    r.add_argument("--accept-self-verified", action="store_true",
                   help="when model 1 also verifies, let its high-confidence links be 'accepted' (default: held for a person)")
    r.add_argument("--docs", nargs="*", help="only documents whose title or id contains one of these")
    r.add_argument("--chunks", nargs="*", help="only these chunk ids")
    r.add_argument("--limit", type=int, help="only the first N chunks")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--no-cache", action="store_true")
    r.set_defaults(fn=cmd_run)

    rv = sub.add_parser("review", help="accept or reject a run's proposed links")
    rv.add_argument("--run", required=True)
    g = rv.add_mutually_exclusive_group(required=True)
    g.add_argument("--accept", action="store_true")
    g.add_argument("--reject", action="store_true")
    rv.add_argument("--min-confidence", choices=["high", "medium"], default="medium")
    rv.set_defaults(fn=cmd_review)

    rs = sub.add_parser("restore", help="undo a run: retract its links and bring back what it superseded")
    rs.add_argument("--run", required=True)
    rs.set_defaults(fn=cmd_restore)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (LLMNotConfigured, ValueError) as exc:
        print(f"Not possible: {exc}", file=sys.stderr)
    except DatabaseUnavailable as exc:
        print(f"Neo4j is not reachable: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
