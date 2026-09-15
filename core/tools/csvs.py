"""Operations on an uploaded CSV, held as parsed rows rather than raw text.

Stdlib csv module only, not pandas: row/column filtering, summarizing, and
single-column aggregation do not need a dataframe library, and one more
large dependency is not worth it for what this actually does. Revisit if a
real request needs something stdlib genuinely cannot do (joins across
files, pivoting).

A CSV behaves like an image here, not like a text document: it is
something a conversation queries more than once ("filter to X", then "now
sort by Y"), so it is held server-side across messages rather than folded
into context and popped on first use.
"""
import csv
import io
import time
import hashlib
from pathlib import Path
from typing import Any

from .errors import ToolError

DIR = Path("data/artifacts")
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 200_000
MAX_PREVIEW_ROWS = 20
MAX_OUTPUT_ROWS = 50_000

_COMPARATORS = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    "contains": lambda a, b: b.lower() in a.lower(),
}


class CsvError(ToolError):
    pass


def _num(v: str) -> float | None:
    try:
        return float(v.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def parse(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    if len(data) > MAX_BYTES:
        raise CsvError(f"file too large, limit {MAX_BYTES // 1024 // 1024}MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except Exception as exc:
            raise CsvError(f"could not decode file as text: "
                           f"{type(exc).__name__}") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvError("no header row found")
    rows = []
    for i, row in enumerate(reader):
        if i >= MAX_ROWS:
            break
        rows.append(row)
    if not rows:
        raise CsvError("no data rows found, only a header")
    return list(reader.fieldnames), rows


def summarize(data: bytes) -> dict[str, Any]:
    columns, rows = parse(data)
    stats = {}
    for col in columns:
        values = [r.get(col, "") for r in rows]
        numeric = [_num(v) for v in values]
        numeric = [n for n in numeric if n is not None]
        if len(numeric) >= len(values) * 0.8 and numeric:
            stats[col] = {"type": "numeric", "min": min(numeric),
                          "max": max(numeric),
                          "mean": round(sum(numeric) / len(numeric), 4)}
        else:
            non_empty = [v for v in values if v.strip()]
            distinct = len(set(non_empty))
            stats[col] = {"type": "text", "distinct_values": distinct,
                          "example": non_empty[0] if non_empty else ""}
    return {"columns": columns, "row_count": len(rows),
           "column_stats": stats,
           "preview": rows[:MAX_PREVIEW_ROWS]}


def filter_rows(data: bytes, column: str, op: str, value: str,
               out_format: str = "preview") -> dict[str, Any]:
    columns, rows = parse(data)
    if column not in columns:
        raise CsvError(f"no column {column!r}. Columns are: "
                       f"{', '.join(columns)}")
    cmp = _COMPARATORS.get(op)
    if not cmp:
        raise CsvError(f"unknown operator {op!r}. Use one of: "
                       f"{', '.join(sorted(_COMPARATORS))}")

    numeric_value = _num(value)
    matched = []
    for row in rows:
        cell = row.get(column, "")
        try:
            if op == "contains":
                ok = cmp(cell, value)
            elif numeric_value is not None and _num(cell) is not None:
                ok = cmp(_num(cell), numeric_value)
            else:
                ok = cmp(cell, value)
        except TypeError:
            ok = False
        if ok:
            matched.append(row)

    result = {"column": column, "op": op, "value": value,
             "matched": len(matched), "of": len(rows),
             "preview": matched[:MAX_PREVIEW_ROWS]}
    if out_format == "file" and matched:
        result["file"] = _write_csv(columns, matched[:MAX_OUTPUT_ROWS], "filter")
    return result


def aggregate(data: bytes, column: str, op: str,
             group_by: str = "") -> dict[str, Any]:
    columns, rows = parse(data)
    if column not in columns:
        raise CsvError(f"no column {column!r}. Columns are: "
                       f"{', '.join(columns)}")
    if group_by and group_by not in columns:
        raise CsvError(f"no column {group_by!r}. Columns are: "
                       f"{', '.join(columns)}")
    if op not in ("sum", "avg", "min", "max", "count"):
        raise CsvError("op must be one of: sum, avg, min, max, count")

    def _compute(values: list[str]) -> Any:
        if op == "count":
            return len(values)
        nums = [n for n in (_num(v) for v in values) if n is not None]
        if not nums:
            raise CsvError(f"column {column!r} has no numeric values to {op}")
        if op == "sum":
            return round(sum(nums), 4)
        if op == "avg":
            return round(sum(nums) / len(nums), 4)
        if op == "min":
            return min(nums)
        return max(nums)

    if not group_by:
        return {"column": column, "op": op,
                "result": _compute([r.get(column, "") for r in rows])}

    groups: dict[str, list[str]] = {}
    for row in rows:
        key = row.get(group_by, "")
        groups.setdefault(key, []).append(row.get(column, ""))
    # Not "results": that key means "a list of hits" everywhere else in this
    # registry (search_web's own shape), and _summarise_result in harness.py
    # indexes it as a list unconditionally. A dict under the same key broke
    # that with a KeyError on the first grouped aggregate ever run.
    return {"column": column, "op": op, "group_by": group_by,
           "by_group": {k: _compute(v) for k, v in groups.items()}}


def _write_csv(columns: list[str], rows: list[dict], stem: str) -> dict[str, Any]:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    out_bytes = buf.getvalue().encode()

    DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(out_bytes).hexdigest()[:8]
    name = f"{stem}-{digest}.csv"
    (DIR / name).write_bytes(out_bytes)
    return {"url": f"/artifacts/{name}", "rows": len(rows),
           "bytes": len(out_bytes), "created": time.time()}
