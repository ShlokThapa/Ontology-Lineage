"""Data-quality scorecard over the lakehouse copy.

Every number comes from SQL run against the in-memory copy (see ``lakehouse.py``): the model never computes
or estimates a score, it only explains the result. The checks are generic (they look at column names and types
rather than hard-coding tables), plus a short list of cross-column business rules that apply wherever the
columns exist.

Five dimensions, each scored 0-100 as the share of checks that passed:

* completeness - is the field filled in (blank text counts as missing)
* uniqueness   - does the key repeat, is a whole row repeated
* validity     - values that make sense (no negatives, percentages in range, dates not in the future, ...)
* consistency  - agrees with itself and with the other tables (orphans, same attribute differing between tables)
* timeliness   - how recently the data was last updated
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import duckdb

from .lakehouse import Snapshot, TableInfo

DIMENSIONS: dict[str, dict[str, Any]] = {
    "completeness": {"label": "Completeness", "weight": 25, "question": "Are the fields filled in?"},
    "uniqueness": {"label": "Uniqueness", "weight": 20, "question": "Is anything recorded twice?"},
    "validity": {"label": "Validity", "weight": 25, "question": "Do the values make sense?"},
    "consistency": {"label": "Consistency", "weight": 20, "question": "Do the tables agree with each other?"},
    "timeliness": {"label": "Timeliness", "weight": 10, "question": "Is the data up to date?"},
}

# How a table is called in plain words. Anything not listed falls back to its own name.
_FRIENDLY = {
    "dim.customer": "customers",
    "dim.account": "customer accounts",
    "dim.branch": "branch account records",
    "fact.customer_monthly_txn": "monthly customer transaction summaries",
    "fact.loan_portfolio": "loans",
}

_NUMERIC = re.compile(r"^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UBIGINT|UINTEGER|DECIMAL|DOUBLE|FLOAT|REAL)", re.I)
_TEXT = re.compile(r"^(VARCHAR|TEXT|STRING)", re.I)
_TIME = re.compile(r"^(DATE|TIMESTAMP)", re.I)
_ID_LIKE = re.compile(r"(^|_)(id|no|number)$", re.I)
_NEVER_NEGATIVE = re.compile(r"(balance|amount|principal|outstanding|credit|debit|count)", re.I)
_NOT_FUTURE = re.compile(r"(created|updated|opened|birth|dob|month)", re.I)
_EMAIL = re.compile(r"e?mail", re.I)

# Cross-column rules, applied wherever every column they name exists. (columns, applies-when, broken-when, text)
_ROW_RULES: list[dict[str, Any]] = [
    {"cols": ["outstanding_amount", "principal_amount"], "bad": "t.outstanding_amount > t.principal_amount",
     "title": "{n} of {total} {entity} owe more than was originally lent", "dim": "validity",
     "detail": "The outstanding amount is larger than the principal."},
    {"cols": ["loan_status", "risk_bucket"], "when": "upper(trim(t.loan_status)) = 'NPA'", "bad": "upper(trim(t.risk_bucket)) NOT LIKE 'HIGH%'",
     "title": "{n} of {total} non-performing {entity} are not rated high risk", "dim": "consistency",
     "detail": "Loans marked NPA (non-performing) should normally carry the highest risk rating."},
    {"cols": ["total_credit", "total_debit", "net_flow"], "bad": "abs(t.net_flow - (t.total_credit - t.total_debit)) > 0.01",
     "title": "{n} of {total} {entity} have a net flow that is not credits minus debits", "dim": "consistency",
     "detail": "net flow should equal total credit minus total debit."},
    {"cols": ["upi_txn_count", "txn_count"], "bad": "t.upi_txn_count > t.txn_count",
     "title": "{n} of {total} {entity} show more UPI transactions than transactions in total", "dim": "validity",
     "detail": "A subset cannot be larger than the whole."},
    {"cols": ["repaid_pct", "outstanding_amount", "principal_amount"], "when": "t.principal_amount > 0",
     "bad": "abs(t.repaid_pct - (1 - t.outstanding_amount / t.principal_amount) * 100) > 0.5",
     "title": "{n} of {total} {entity} have a repaid percentage that does not match what is still owed", "dim": "consistency",
     "detail": "repaid % should equal 1 - outstanding / principal."},
    {"cols": ["loan_status", "outstanding_amount"], "when": "upper(trim(t.loan_status)) = 'CLOSED'", "bad": "t.outstanding_amount > 0",
     "title": "{n} of {total} closed {entity} still show an amount outstanding", "dim": "consistency",
     "detail": "A closed loan should have nothing left to pay."},
    {"cols": ["created_at", "updated_at"], "bad": "t.updated_at < t.created_at",
     "title": "{n} of {total} {entity} were updated before they were created", "dim": "validity",
     "detail": "The update time is earlier than the creation time."},
]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _ref(t: TableInfo) -> str:
    return f"{_q(t.schema)}.{_q(t.name)}"


def _words(col: str) -> str:
    text = re.sub(r"_masked$", "", col).replace("_", " ").strip()
    text = re.sub(r"\bid\b", "ID", text, flags=re.I)
    text = re.sub(r"\bno\b", "number", text, flags=re.I)
    text = re.sub(r"\bpct\b", "%", text, flags=re.I)
    text = re.sub(r"\bdob\b", "date of birth", text, flags=re.I)
    return text


def _a(words: str) -> str:
    return ("an " if words[:1].lower() in "aeiou" else "a ") + words


def _entity(t: TableInfo) -> str:
    return _FRIENDLY.get(t.full_name.lower(), f"rows in {t.full_name}")


def _family(dtype: str) -> str:
    if _NUMERIC.match(dtype):
        return "number"
    if _TEXT.match(dtype):
        return "text"
    if _TIME.match(dtype):
        return "time"
    return "other"


def _severity(pct: float, floor: str = "low") -> str:
    level = "high" if pct >= 10 else "medium" if pct >= 2 else "low"
    order = ["low", "medium", "high"]
    return order[max(order.index(level), order.index(floor))]


def _num(x: Any) -> float | int | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) and abs(f) < 1e15 else round(f, 2)


@dataclass
class Result:
    dimension: str
    table: str
    title: str
    checked: int
    failed: int
    column: str | None = None
    detail: str = ""
    examples: list[Any] = field(default_factory=list)
    severity: str = "low"

    @property
    def pct(self) -> float:
        return 100.0 * self.failed / self.checked if self.checked else 0.0


class _Profiler:
    def __init__(self, snap: Snapshot, only: TableInfo | None):
        self.snap = snap
        self.only = only
        self.cur = snap.cursor()
        self.results: list[Result] = []
        self.keys: dict[str, list[str]] = {}
        self.now = datetime.now(timezone.utc).replace(tzinfo=None)

    # -- helpers --------------------------------------------------------------------------------

    def _one(self, sql: str, params: list[Any] | None = None) -> tuple[Any, ...]:
        return self.cur.execute(sql, params or []).fetchone() or ()

    def _all(self, sql: str) -> list[tuple[Any, ...]]:
        return self.cur.execute(sql).fetchall()

    def _key_expr(self, t: TableInfo, alias: str = "t") -> str:
        key = self.keys.get(t.full_name)
        if not key:
            return "NULL"
        return " || '/' || ".join(f"CAST({alias}.{_q(k)} AS VARCHAR)" for k in key)

    def _examples(self, t: TableInfo, where: str, col: str | None = None) -> list[Any]:
        key = self.keys.get(t.full_name)
        try:
            if key:
                rows = self._all(f"SELECT {self._key_expr(t)} FROM {_ref(t)} t WHERE {where} LIMIT 3")
                return [f"{'/'.join(key)} {r[0]}" for r in rows]
            if col:
                rows = self._all(f"SELECT t.{_q(col)} FROM {_ref(t)} t WHERE {where} LIMIT 3")
                return [str(r[0]) for r in rows]
        except duckdb.Error:
            pass
        return []

    def row_check(self, dim: str, t: TableInfo, title: str, bad: str, applicable: str = "TRUE", column: str | None = None,
                  detail: str = "", floor: str = "low") -> Result | None:
        try:
            n, f = self._one(
                f"SELECT count(*) FILTER (WHERE {applicable}), count(*) FILTER (WHERE ({applicable}) AND ({bad})) FROM {_ref(t)} t"
            )
        except duckdb.Error:
            return None
        if not n:
            return None
        res = Result(dim, t.full_name, title.format(n=f, total=n, entity=_entity(t)), int(n), int(f), column, detail)
        if f:
            res.examples = self._examples(t, f"({applicable}) AND ({bad})", column)
            res.severity = _severity(res.pct, floor)
        self.results.append(res)
        return res

    # -- keys -----------------------------------------------------------------------------------

    def infer_keys(self) -> None:
        for t in self.snap.tables:
            names = [c[0] for c in t.columns]
            rows = max(t.loaded, 1)
            best: tuple[float, str] | None = None
            for c, dtype in t.columns:
                if not _ID_LIKE.search(c) or _family(dtype) == "time":
                    continue
                n, d = self._one(f"SELECT count({_q(c)}), count(DISTINCT {_q(c)}) FROM {_ref(t)}")
                ratio = d / rows if n == rows else 0.0
                if ratio >= 0.9 and (best is None or ratio > best[0]):
                    best = (ratio, c)
            if best:
                self.keys[t.full_name] = [best[1]]
                continue
            times = [c for c, dtype in t.columns if _family(dtype) == "time"]
            ids = [c for c in names if _ID_LIKE.search(c)]
            for i in ids:
                for tm in times:
                    d = self._one(f"SELECT count(*) FROM (SELECT DISTINCT {_q(i)}, {_q(tm)} FROM {_ref(t)})")[0]
                    if d / rows >= 0.9:
                        self.keys[t.full_name] = [i, tm]
                        break
                if t.full_name in self.keys:
                    break

    # -- the five dimensions --------------------------------------------------------------------

    def completeness(self, t: TableInfo) -> None:
        for c, dtype in t.columns:
            blank = f"(t.{_q(c)} IS NULL OR trim(CAST(t.{_q(c)} AS VARCHAR)) = '')" if _family(dtype) == "text" else f"t.{_q(c)} IS NULL"
            self.row_check("completeness", t, "{n} of {total} {entity} have no " + _words(c), blank, column=c)

    def uniqueness(self, t: TableInfo) -> None:
        key = self.keys.get(t.full_name)
        if key:
            expr = ", ".join(f"t.{_q(k)}" for k in key)
            n, d = self._one(f"SELECT count(*), count(DISTINCT ({expr})) FROM {_ref(t)} t")
            dup = int(n - d)
            label = " + ".join(_words(k) for k in key)
            res = Result("uniqueness", t.full_name, f"{dup} of {n} {_entity(t)} repeat an existing {label}", int(n), dup, key[0],
                         f"{label} should identify each row once.")
            if dup:
                res.severity = _severity(res.pct, "medium")
                res.examples = [str(r[0]) for r in self._all(
                    f"SELECT {self._key_expr(t)} FROM {_ref(t)} t GROUP BY {expr} HAVING count(*) > 1 LIMIT 3")] if len(key) == 1 else []
            self.results.append(res)
        cols = ", ".join(_q(c[0]) for c in t.columns)
        n, d = self._one(f"SELECT count(*), count(*) - count(DISTINCT ({cols})) FROM {_ref(t)}") if len(t.columns) < 60 else (0, 0)
        if n:
            dup = int(d)
            res = Result("uniqueness", t.full_name, f"{dup} of {n} {_entity(t)} are exact copies of another row", int(n), dup, None,
                         "Every column is identical to another row.")
            if dup:
                res.severity = _severity(res.pct, "medium")
            self.results.append(res)

    def validity(self, t: TableInfo) -> None:
        for c, dtype in t.columns:
            fam, lc = _family(dtype), c.lower()
            if fam == "number":
                if _NEVER_NEGATIVE.search(lc) and not lc.startswith("net_"):
                    self.row_check("validity", t, "{n} of {total} {entity} have a negative " + _words(c), f"t.{_q(c)} < 0",
                                   f"t.{_q(c)} IS NOT NULL", c, f"{_words(c).capitalize()} should not be below zero.")
                if re.search(r"(pct|percent)", lc):
                    self.row_check("validity", t, "{n} of {total} {entity} have " + _a(_words(c)) + " outside 0 to 100",
                                   f"t.{_q(c)} < 0 OR t.{_q(c)} > 100", f"t.{_q(c)} IS NOT NULL", c, "A percentage must be between 0 and 100.")
                if re.search(r"(interest_)?rate$", lc):
                    self.row_check("validity", t, "{n} of {total} {entity} have an implausible " + _words(c),
                                   f"t.{_q(c)} < 0 OR t.{_q(c)} > 50", f"t.{_q(c)} IS NOT NULL", c, "Rates are expected between 0 and 50 percent.")
            elif fam == "time" and _NOT_FUTURE.search(lc):
                self.row_check("validity", t, "{n} of {total} {entity} have " + _a(_words(c)) + " in the future",
                               f"t.{_q(c)} > now()::TIMESTAMP", f"t.{_q(c)} IS NOT NULL", c, "This date has not happened yet.")
                if re.search(r"(birth|dob)", lc):
                    self.row_check("validity", t, "{n} of {total} {entity} have an implausible age",
                                   f"date_diff('year', t.{_q(c)}, now()::TIMESTAMP) NOT BETWEEN 18 AND 110", f"t.{_q(c)} IS NOT NULL", c,
                                   "Customers are expected to be between 18 and 110 years old.")
            elif fam == "text":
                if _EMAIL.search(lc):
                    self.row_check("validity", t, "{n} of {total} {entity} have " + _a(_words(c)) + " that is not an email address",
                                   f"NOT regexp_matches(t.{_q(c)}, '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$')", f"t.{_q(c)} IS NOT NULL AND trim(t.{_q(c)}) <> ''", c,
                                   "An email address needs an @ and a domain.")
                self.row_check("validity", t, "{n} of {total} {entity} have extra spaces in " + _words(c), f"t.{_q(c)} <> trim(t.{_q(c)})",
                               f"t.{_q(c)} IS NOT NULL", c, "Leading or trailing spaces make matching and reporting unreliable.")
        have = {c[0] for c in t.columns}
        for rule in _ROW_RULES:
            if all(c in have for c in rule["cols"]):
                self.row_check(rule["dim"], t, rule["title"], rule["bad"], rule.get("when", "TRUE"), rule["cols"][0], rule["detail"], floor="medium")

    def _grouped(self, t: TableInfo, title: str, dim: str, col: str, cte: str, bad_join: str, checked: str, detail: str) -> None:
        """A check defined by a CTE named ``m`` (one row per group, with the expected value in ``top``)."""
        try:
            n = self._one(f"SELECT count(*) FROM {_ref(t)} t WHERE {checked}")[0]
            f = self._one(f"WITH {cte} SELECT count(*) FROM {_ref(t)} t JOIN m ON {bad_join}")[0]
        except duckdb.Error:
            return
        if not n:
            return
        res = Result(dim, t.full_name, title.format(n=f, total=n, entity=_entity(t)), int(n), int(f), col, detail)
        if f:
            res.severity = _severity(res.pct, "low")
            try:
                res.examples = [str(r[0]) for r in self._all(
                    f"WITH {cte} SELECT {self._key_expr(t)} FROM {_ref(t)} t JOIN m ON {bad_join} LIMIT 3")] if self.keys.get(t.full_name) else []
            except duckdb.Error:
                pass
        self.results.append(res)

    def labels(self, t: TableInfo) -> None:
        """Category columns: the same value spelled two ways, and a code that does not match its label."""
        names = {x[0] for x in t.columns}
        for c, dtype in t.columns:
            if _family(dtype) != "text" or t.loaded < 20:
                continue
            n, d = self._one(f"SELECT count({_q(c)}), count(DISTINCT {_q(c)}) FROM {_ref(t)}")
            if not n or d > 12 or d == n:
                continue
            qc = _q(c)
            cte = (f"g AS (SELECT lower(trim({qc})) k, {qc} v, count(*) n FROM {_ref(t)} WHERE {qc} IS NOT NULL GROUP BY 1, 2), "
                   f"m AS (SELECT k, arg_max(v, n) top FROM g GROUP BY k HAVING count(*) > 1)")
            self._grouped(t, "{n} of {total} {entity} spell " + _words(c) + " differently from the rest", "consistency", c, cte,
                          f"lower(trim(t.{qc})) = m.k AND t.{qc} <> m.top", f"t.{qc} IS NOT NULL",
                          "The same value is written in more than one way (case or spacing).")
            for sib in (f"{c}_flag", f"{c}_code", f"{c}_id"):
                if sib in names:
                    qs = _q(sib)
                    cte2 = (f"g AS (SELECT {qc} v, {qs} s, count(*) n FROM {_ref(t)} WHERE {qc} IS NOT NULL GROUP BY 1, 2), "
                            f"m AS (SELECT v, arg_max(s, n) top FROM g GROUP BY v)")
                    self._grouped(t, "{n} of {total} {entity} have " + _a(_words(sib)) + " that does not match " + _words(c), "consistency", sib,
                                  cte2, f"t.{qc} = m.v AND t.{qs} IS DISTINCT FROM m.top", f"t.{qc} IS NOT NULL",
                                  f"Each {_words(c)} should always carry the same {_words(sib)}.")

    def relationships(self) -> None:
        tables = self.snap.tables
        for child in tables:
            for c, _ in child.columns:
                for parent in tables:
                    pkey = self.keys.get(parent.full_name)
                    if parent is child or not pkey or len(pkey) != 1 or pkey[0] != c:
                        continue
                    if self.only and self.only is not child and self.only is not parent:
                        continue
                    self.row_check("consistency", child,
                                   "{n} of {total} {entity} point to a " + _words(c) + " that does not exist in " + parent.full_name,
                                   f"t.{_q(c)} NOT IN (SELECT {_q(c)} FROM {_ref(parent)})", f"t.{_q(c)} IS NOT NULL", c,
                                   f"Every {_words(c)} here should exist in {parent.full_name}.", floor="medium")
                    self._shared_attributes(child, parent, c)

    def _shared_attributes(self, child: TableInfo, parent: TableInfo, key: str) -> None:
        pcols = {n: dt for n, dt in parent.columns}
        for c, dtype in child.columns:
            if c == key or c not in pcols or _family(dtype) != _family(pcols[c]) or _family(dtype) not in ("text", "number"):
                continue
            if re.search(r"(created|updated|_at$|_date$|^status$)", c.lower()):
                continue
            both = f"c.{_q(c)} IS NOT NULL AND p.{_q(c)} IS NOT NULL"
            diff = (f"lower(trim(c.{_q(c)})) <> lower(trim(p.{_q(c)}))" if _family(dtype) == "text"
                    else f"abs(c.{_q(c)} - p.{_q(c)}) > 0.005")
            self._join_check(child, parent, key, c, both, diff, c)
        if "customer_name" in {n for n, _ in child.columns} and {"first_name", "last_name"} <= set(pcols):
            self._join_check(child, parent, key, "customer_name", "c.customer_name IS NOT NULL",
                             "lower(trim(c.customer_name)) <> lower(trim(p.first_name || ' ' || p.last_name))", "customer_name")

    def _join_check(self, child: TableInfo, parent: TableInfo, key: str, col: str, both: str, diff: str, label_col: str) -> None:
        try:
            n, f = self._one(
                f"SELECT count(*) FILTER (WHERE {both}), count(*) FILTER (WHERE ({both}) AND ({diff})) "
                f"FROM {_ref(child)} c JOIN {_ref(parent)} p ON c.{_q(key)} = p.{_q(key)}"
            )
        except duckdb.Error:
            return
        if not n:
            return
        res = Result("consistency", child.full_name,
                     f"{f} of {n} {_entity(child)} show a different {_words(label_col)} than {parent.full_name}", int(n), int(f), label_col,
                     f"{_words(label_col).capitalize()} should be the same wherever {_words(key)} matches.")
        if f:
            res.severity = _severity(res.pct, "medium")
            try:
                res.examples = [f"{_words(key)} {r[0]}" for r in self._all(
                    f"SELECT c.{_q(key)} FROM {_ref(child)} c JOIN {_ref(parent)} p ON c.{_q(key)} = p.{_q(key)} WHERE ({both}) AND ({diff}) LIMIT 3")]
            except duckdb.Error:
                pass
        self.results.append(res)

    def business_rules(self) -> None:
        """Rules that span two tables. Applied only where the columns exist."""
        for child in self.snap.tables:
            names = {c[0] for c in child.columns}
            status = next((c for c in ("account_status", "status") if c in names), None)
            if not status or "customer_id" not in names or (self.only and self.only is not child):
                continue
            for parent in self.snap.tables:
                if parent is child or self.keys.get(parent.full_name) != ["customer_id"] or "kyc_status" not in {c[0] for c in parent.columns}:
                    continue
                kyc = (f"c.{_q('customer_id')} IN (SELECT customer_id FROM {_ref(parent)} WHERE upper(trim(kyc_status)) <> 'VERIFIED')")
                bad = f"upper(trim(c.{_q(status)})) = 'ACTIVE' AND {kyc}"
                try:
                    n = self._one(f"SELECT count(*) FROM {_ref(child)} c WHERE upper(trim(c.{_q(status)})) = 'ACTIVE'")[0]
                    f = self._one(f"SELECT count(*) FROM {_ref(child)} c WHERE {bad}")[0]
                except duckdb.Error:
                    continue
                if not n:
                    continue
                res = Result("consistency", child.full_name, f"{f} of {n} active {_entity(child)} belong to customers whose KYC is not verified",
                             int(n), int(f), status, "Active accounts should only exist for customers with verified KYC (expired or pending is a compliance gap).")
                if f:
                    res.severity = "high" if res.pct >= 5 else "medium"
                    res.examples = [f"account {r[0]}" for r in self._all(
                        f"SELECT c.{_q(next((x for x in ('account_no', 'account_id') if x in names), 'customer_id'))} FROM {_ref(child)} c WHERE {bad} LIMIT 3")]
                self.results.append(res)
                return

    def freshness(self) -> list[dict[str, Any]]:
        out = []
        for t in self.snap.tables:
            if self.only and self.only is not t:
                continue
            best: tuple[str, datetime] | None = None
            for c, dtype in t.columns:
                if _family(dtype) == "time" and re.search(r"(updated|modified|txn_month|created)", c.lower()):
                    latest = self._one(f"SELECT max({_q(c)}) FILTER (WHERE {_q(c)} <= now()::TIMESTAMP) FROM {_ref(t)}")[0]
                    if latest and (best is None or latest > best[1]):
                        best = (c, latest)
            if best:
                age = max((self.now - best[1]).days, 0)
                score = 100 if age <= 35 else 85 if age <= 100 else 60 if age <= 370 else 30
                out.append({"table": t.full_name, "column": best[0], "latest": best[1].date().isoformat(), "age_days": age, "score": score})
        return out


# A message about the quality or health of the data. \b on both sides keeps "quality" inside other words from matching.
_QUALITY_WORDS = re.compile(
    r"\b(data[ -]?quality|dq|quality|completeness|incomplete|missing|blanks?|nulls?|duplicat\w*|accura\w*|inaccura\w*|"
    r"clean(ness|liness|ing)?|dirty|healthy|health|validity|invalid|integrity|orphan\w*|consisten\w*|inconsisten\w*|"
    r"scorecard|profil(e|ing)|trustworth\w*|reliab\w*|reliable|bad data|data issues?|weakest|errors?)\b",
    re.IGNORECASE,
)


def wants_quality(text: str) -> bool:
    """True when the message is about the quality or health of the data, which is when the scorecard tool is offered."""
    return bool(_QUALITY_WORDS.search(text or ""))


def _grade(score: float | None) -> str:
    if score is None:
        return "No checks"
    return "Excellent" if score >= 97 else "Good" if score >= 92 else "Fair" if score >= 80 else "Needs attention" if score >= 60 else "Poor"


def _capped_grade(score: float | None, high: int, medium: int) -> str:
    """A high score does not hide serious findings: any high-severity finding stops the grade at Fair."""
    order = ["Poor", "Needs attention", "Fair", "Good", "Excellent"]
    grade = _grade(score)
    if grade not in order:
        return grade
    ceiling = "Needs attention" if high >= 3 else "Fair" if high >= 1 else "Good" if medium >= 3 else "Excellent"
    return order[min(order.index(grade), order.index(ceiling))]


def _pool(results: list[Result]) -> tuple[float | None, int, int]:
    checked = sum(r.checked for r in results)
    failed = sum(r.failed for r in results)
    return (round(100.0 * (checked - failed) / checked, 1) if checked else None), checked, failed


def build_report(snap: Snapshot, table: str | None = None) -> dict[str, Any]:
    """The full scorecard as plain data. ``table`` limits it to one table (cross-table checks that involve it still run)."""
    only = snap.table(table) if table else None
    if table and only is None:
        raise ValueError(f"No table '{table}'. Tables: {', '.join(t.full_name for t in snap.tables)}")
    p = _Profiler(snap, only)
    p.infer_keys()
    scope = [only] if only else snap.tables
    for t in scope:
        p.completeness(t)
        p.uniqueness(t)
        p.validity(t)
        p.labels(t)
    p.relationships()
    p.business_rules()
    fresh = p.freshness()

    by_dim: dict[str, list[Result]] = {d: [r for r in p.results if r.dimension == d] for d in DIMENSIONS}
    dimensions = []
    for key, meta in DIMENSIONS.items():
        if key == "timeliness":
            score = round(sum(f["score"] for f in fresh) / len(fresh), 1) if fresh else None
            checked, failed = len(fresh), sum(1 for f in fresh if f["score"] < 85)
        else:
            score, checked, failed = _pool(by_dim[key])
        dimensions.append({"key": key, "label": meta["label"], "question": meta["question"], "score": score, "checked": checked,
                           "failed": failed, "weight": meta["weight"]})
    scored = [d for d in dimensions if d["score"] is not None]
    overall = round(sum(d["score"] * d["weight"] for d in scored) / sum(d["weight"] for d in scored), 1) if scored else None

    tables = []
    for t in scope:
        mine = [r for r in p.results if r.table == t.full_name]
        t_dims = {}
        for key in DIMENSIONS:
            if key == "timeliness":
                f = next((x for x in fresh if x["table"] == t.full_name), None)
                t_dims[key] = f["score"] if f else None
            else:
                t_dims[key] = _pool([r for r in mine if r.dimension == key])[0]
        t_scored = [(DIMENSIONS[k]["weight"], v) for k, v in t_dims.items() if v is not None]
        t_score = round(sum(w * v for w, v in t_scored) / sum(w for w, _ in t_scored), 1) if t_scored else None
        f = next((x for x in fresh if x["table"] == t.full_name), None)
        tables.append({
            "name": t.full_name, "label": _entity(t), "rows": t.rows, "columns": len(t.columns), "score": t_score, "grade": _capped_grade(t_score, sum(1 for r in mine if r.failed and r.severity == "high"),
                                   sum(1 for r in mine if r.failed and r.severity == "medium")),
            "dimensions": t_dims, "issues": sum(1 for r in mine if r.failed), "sampled": t.sampled,
            "latest": f["latest"] if f else None, "key": p.keys.get(t.full_name),
        })
    tables.sort(key=lambda x: (x["score"] is None, x["score"] if x["score"] is not None else 0))

    order = {"high": 0, "medium": 1, "low": 2}
    failing = sorted((r for r in p.results if r.failed), key=lambda r: (order[r.severity], -r.pct, -r.failed))
    issues = [{
        "severity": r.severity, "dimension": r.dimension, "table": r.table, "column": r.column, "title": r.title, "detail": r.detail,
        "failed": r.failed, "checked": r.checked, "pct": round(r.pct, 1), "examples": r.examples,
    } for r in failing[:30]]

    return {
        "title": "Data quality scorecard",
        "scope": only.full_name if only else f"All {len(scope)} tables",
        "as_of": snap.as_of.isoformat(timespec="seconds"),
        "overall": {
            "score": overall, "grade": _capped_grade(overall, sum(1 for r in failing if r.severity == "high"),
                                                    sum(1 for r in failing if r.severity == "medium")), "tables": len(scope), "rows": sum(t.rows for t in scope),
            "columns": sum(len(t.columns) for t in scope), "checks": len(p.results) + len(fresh),
            "passed": sum(1 for r in p.results if not r.failed) + sum(1 for f in fresh if f["score"] >= 85),
            "issues": len(failing), "high": sum(1 for r in failing if r.severity == "high"),
            "medium": sum(1 for r in failing if r.severity == "medium"), "low": sum(1 for r in failing if r.severity == "low"),
        },
        "dimensions": dimensions, "tables": tables, "issues": issues, "freshness": fresh,
        "not_read": snap.skipped,
    }


def summary_for_model(report: dict[str, Any], top: int = 8) -> dict[str, Any]:
    """What the model gets to read: the headline numbers and the worst findings, not the whole payload."""
    o = report["overall"]
    return {
        "dashboard_displayed": True,
        "scope": report["scope"],
        "overall_score": o["score"], "grade": o["grade"], "checks_run": o["checks"], "issues_found": o["issues"],
        "issues_by_severity": {"high": o["high"], "medium": o["medium"], "low": o["low"]},
        "dimension_scores": {d["label"]: d["score"] for d in report["dimensions"]},
        "table_scores": {t["name"]: t["score"] for t in report["tables"]},
        "worst_issues": [{"severity": i["severity"], "table": i["table"], "finding": i["title"], "examples": i["examples"]} for i in report["issues"][:top]],
        "note": "A scorecard with these numbers is shown to the user under your answer. Explain what matters; do not repeat every number.",
        "count": o["issues"],
    }
