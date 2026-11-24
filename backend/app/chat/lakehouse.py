"""Read-only view of the lakehouse's business tables, for the chat.

The tables are read straight from OneLake over HTTPS (port 443), so this works where the SQL endpoint's port
1433 is blocked. Each Delta table is rebuilt from its transaction log (the files still current), copied into
an in-memory DuckDB, and that copy is what the chat reads. Nothing is ever written back to Fabric, and the copy
is locked down (no file access, one SELECT at a time), so a question can never change data.

The copy is reused for ``chat_data_ttl_seconds`` so a conversation does not re-download the tables on every turn.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import duckdb

from ..config import Settings
from ..jobs.fabric import FabricClient, FabricError

log = logging.getLogger(__name__)

STORAGE_SCOPE = "https://storage.azure.com/.default"
_COMMIT = re.compile(r"(\d{20})\.json")
_CHECKPOINT = re.compile(r"(\d{20})\.checkpoint(?:\.(\d{10})\.(\d{10}))?\.parquet")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_\-./%=+ ]+$")
_IDENT = re.compile(r"^[A-Za-z0-9_ .\-]{1,128}$")

QUERY_TIMEOUT_SECONDS = 10.0
QUERY_MAX_ROWS = 60


class LakehouseError(RuntimeError):
    """Reading the lakehouse failed. The message is safe to show: it never contains a token or a secret."""


@dataclass
class TableInfo:
    schema: str
    name: str
    rows: int  # rows in the lakehouse
    loaded: int  # rows in the copy (less than ``rows`` when the table was capped)
    columns: list[tuple[str, str]] = field(default_factory=list)  # (name, duckdb type)

    @property
    def full_name(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def sampled(self) -> bool:
        return self.loaded < self.rows


class Snapshot:
    """An in-memory copy of the tables at one moment."""

    def __init__(self, con: duckdb.DuckDBPyConnection, tables: list[TableInfo], skipped: dict[str, str], as_of: datetime):
        self._con = con
        self.tables = tables
        self.skipped = skipped  # table -> why it could not be read
        self.as_of = as_of
        self._lock = threading.Lock()

    def table(self, ref: str) -> TableInfo | None:
        """Find a table by 'schema.name', or by name alone when that is unambiguous (case-insensitive)."""
        ref = (ref or "").strip().strip('"').lower()
        exact = [t for t in self.tables if t.full_name.lower() == ref]
        if exact:
            return exact[0]
        named = [t for t in self.tables if t.name.lower() == ref]
        return named[0] if len(named) == 1 else None

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self._con.cursor()

    def query(self, sql: str, max_rows: int = QUERY_MAX_ROWS, timeout: float = QUERY_TIMEOUT_SECONDS) -> dict[str, Any]:
        """Run ONE read-only SELECT against the copy. Anything else is refused."""
        sql = (sql or "").strip()
        if not sql:
            raise LakehouseError("Write a SELECT statement.")
        cur = self.cursor()
        try:
            statements = cur.extract_statements(sql)
        except duckdb.Error as exc:
            raise LakehouseError(f"That is not valid SQL: {str(exc).splitlines()[0][:200]}") from exc
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise LakehouseError("Only a single SELECT statement is allowed.")
        timer = threading.Timer(timeout, cur.interrupt)
        timer.start()
        try:
            cur.execute(sql)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
        except duckdb.InterruptException as exc:
            raise LakehouseError(f"The query ran longer than {timeout:.0f} seconds and was stopped.") from exc
        except duckdb.Error as exc:
            raise LakehouseError(f"The query failed: {str(exc).splitlines()[0][:240]}") from exc
        finally:
            timer.cancel()
        return {"columns": columns, "rows": [list(r) for r in rows[:max_rows]], "truncated": len(rows) > max_rows}


class LakehouseData:
    """Builds and caches the :class:`Snapshot`."""

    def __init__(self, settings: Settings, fabric: FabricClient | None = None, clock: Any = time.time):
        self.s = settings
        self.fabric = fabric or FabricClient(settings)
        self._clock = clock
        self._lock = threading.Lock()
        self._snap: Snapshot | None = None
        self._built_at = 0.0
        self.base = settings.fabric_onelake_url.rstrip("/")

    @property
    def configured(self) -> bool:
        return self.fabric.configured and self.fabric.tokens.mode == "service_principal"

    def snapshot(self, refresh: bool = False) -> Snapshot:
        with self._lock:
            fresh = self._snap is not None and (self._clock() - self._built_at) < self.s.chat_data_ttl_seconds
            if refresh or not fresh:
                try:
                    self._snap = self._build()
                except FabricError as exc:
                    raise LakehouseError(str(exc)) from exc
                self._built_at = self._clock()
            assert self._snap is not None
            return self._snap

    # -- OneLake ----------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.fabric.tokens.token_for(STORAGE_SCOPE)}", "x-ms-version": "2021-06-08"}

    def _get(self, url: str, **kw: Any) -> Any:
        try:
            resp = self.fabric.http.get(url, headers=self._headers(), **kw)
        except Exception as exc:  # noqa: BLE001 - network problems of any kind
            raise LakehouseError(f"Could not reach OneLake ({type(exc).__name__}).") from exc
        if resp.status_code == 403:
            raise LakehouseError("The signed-in identity cannot read the lakehouse files (OneLake access is off, or it is not a workspace member).")
        if resp.status_code == 404:
            raise LakehouseError("A lakehouse path was not found.")
        if resp.status_code >= 400:
            raise LakehouseError(f"OneLake returned HTTP {resp.status_code}.")
        return resp

    def _ls(self, workspace_id: str, directory: str) -> list[str]:
        names: list[str] = []
        token = ""
        for _ in range(50):
            params = {"resource": "filesystem", "recursive": "false", "directory": directory}
            if token:
                params["continuation"] = token
            resp = self._get(f"{self.base}/{workspace_id}", params=params)
            names += [p["name"].rsplit("/", 1)[-1] for p in resp.json().get("paths", [])]
            token = resp.headers.get("x-ms-continuation", "")
            if not token:
                break
        return names

    def _lakehouse(self) -> tuple[str, str]:
        wid = self.fabric.workspace_id()
        wanted = self.s.chat_lakehouse_name.strip().lower()
        found = [i for i in self.fabric.list_items("Lakehouse") if str(i.get("displayName", "")).lower() == wanted]
        if not found:
            raise LakehouseError(f"No lakehouse named '{self.s.chat_lakehouse_name}' in the workspace.")
        return wid, str(found[0]["id"])

    # -- Delta ------------------------------------------------------------------------------------

    def _active_files(self, wid: str, table_dir: str, tmp: Path) -> list[str]:
        """The data files a Delta table currently consists of, by replaying its transaction log."""
        log_dir = f"{table_dir}/_delta_log"
        names = self._ls(wid, log_dir)
        commits = sorted(int(m.group(1)) for n in names if (m := _COMMIT.fullmatch(n)))
        parts: dict[int, list[str]] = {}
        totals: dict[int, int] = {}
        for n in names:
            m = _CHECKPOINT.fullmatch(n)
            if m:
                parts.setdefault(int(m.group(1)), []).append(n)
                totals[int(m.group(1))] = int(m.group(3) or 1)
        complete = [v for v, p in parts.items() if len(p) >= totals[v]]
        active: dict[str, bool] = {}  # path -> has a deletion vector
        start = 0
        if complete:
            ckpt = max(complete)
            local = []
            for i, n in enumerate(sorted(parts[ckpt])):
                dest = tmp / f"ckpt_{i}.parquet"
                dest.write_bytes(self._get(f"{self.base}/{wid}/{log_dir}/{n}").content)
                local.append(str(dest))
            con = duckdb.connect()
            try:
                files = ", ".join("'" + f.replace("'", "''") + "'" for f in local)
                for path, dv in con.execute(f'SELECT "add"."path", "add"."deletionVector" IS NOT NULL FROM read_parquet([{files}]) WHERE "add" IS NOT NULL').fetchall():
                    active[path] = bool(dv)
            finally:
                con.close()
            start = ckpt + 1
        for version in commits:
            if version < start:
                continue
            text = self._get(f"{self.base}/{wid}/{log_dir}/{version:020d}.json").text
            for line in text.splitlines():
                if not line.strip():
                    continue
                action = json.loads(line)
                if "add" in action:
                    active[action["add"]["path"]] = bool(action["add"].get("deletionVector"))
                elif "remove" in action:
                    active.pop(action["remove"]["path"], None)
                elif "metaData" in action:
                    meta = action["metaData"]
                    if meta.get("partitionColumns"):
                        raise LakehouseError("partitioned tables are not supported")
                    if (meta.get("configuration") or {}).get("delta.columnMapping.mode", "none") != "none":
                        raise LakehouseError("tables with column mapping are not supported")
        if any(active.values()):
            raise LakehouseError("tables with deletion vectors are not supported")
        return sorted(active)

    def _load_table(self, con: duckdb.DuckDBPyConnection, wid: str, lid: str, schema: str, name: str) -> TableInfo | None:
        table_dir = f"{lid}/Tables/{schema}/{name}"
        tmp = Path(tempfile.mkdtemp(prefix="lh_"))
        try:
            paths = self._active_files(wid, table_dir, tmp)
            if not paths:
                return None
            local: list[str] = []
            for i, rel in enumerate(paths):
                if ".." in rel or rel.startswith("/") or "://" in rel or not _SAFE_PATH.match(rel):
                    raise LakehouseError("a data file has an unexpected path")
                dest = tmp / f"p{i}.parquet"
                dest.write_bytes(self._get(f"{self.base}/{wid}/{quote(table_dir, safe='/')}/{rel}").content)
                local.append(str(dest))
            files = ", ".join("'" + f.replace("'", "''") + "'" for f in local)
            source = f"read_parquet([{files}], union_by_name=true)"
            total = int(con.execute(f"SELECT count(*) FROM {source}").fetchone()[0])
            limit = self.s.chat_data_max_rows
            q_schema, q_name = '"' + schema.replace('"', '""') + '"', '"' + name.replace('"', '""') + '"'
            # Timestamps with a time zone are stored as plain UTC timestamps: reading them back otherwise needs the
            # tzdata package, which Windows does not have.
            select = ", ".join(
                f'"{c[0].replace(chr(34), chr(34) * 2)}"::TIMESTAMP AS "{c[0].replace(chr(34), chr(34) * 2)}"'
                if "TIME ZONE" in str(c[1]).upper()
                else f'"{c[0].replace(chr(34), chr(34) * 2)}"'
                for c in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
            )
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {q_schema}")
            con.execute(f"CREATE TABLE {q_schema}.{q_name} AS SELECT {select} FROM {source} LIMIT {limit}")
            cols = [(r[0], r[1]) for r in con.execute(
                "SELECT column_name, data_type FROM duckdb_columns() WHERE schema_name = ? AND table_name = ? ORDER BY column_index",
                [schema, name],
            ).fetchall()]
            return TableInfo(schema, name, total, min(total, limit), cols)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _build(self) -> Snapshot:
        if not self.configured:
            raise LakehouseError("The lakehouse is not connected: a service principal and a workspace are needed.")
        wid, lid = self._lakehouse()
        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        tables: list[TableInfo] = []
        skipped: dict[str, str] = {}
        for schema in [s.strip() for s in self.s.chat_data_schemas.split(",") if s.strip()]:
            if not _IDENT.match(schema):
                continue
            try:
                names = self._ls(wid, f"{lid}/Tables/{schema}")
            except LakehouseError as exc:
                skipped[schema] = str(exc)
                continue
            for name in sorted(names):
                if not _IDENT.match(name) or name.startswith("_"):
                    continue
                try:
                    info = self._load_table(con, wid, lid, schema, name)
                except LakehouseError as exc:
                    skipped[f"{schema}.{name}"] = str(exc)
                    continue
                except duckdb.Error as exc:
                    skipped[f"{schema}.{name}"] = f"could not be read ({str(exc).splitlines()[0][:120]})"
                    continue
                if info:
                    tables.append(info)
        if not tables:
            raise LakehouseError("No tables could be read from the lakehouse. " + "; ".join(f"{k}: {v}" for k, v in skipped.items()))
        # From here on the copy can only be read: no files, no settings changes.
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        log.info("Lakehouse copy built: %s", ", ".join(f"{t.full_name} ({t.loaded} rows)" for t in tables))
        return Snapshot(con, tables, skipped, datetime.now(timezone.utc))
