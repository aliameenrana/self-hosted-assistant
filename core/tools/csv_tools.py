"""CSV tools, bound to one request's uploaded spreadsheets.

Same shape as image_tools.build(): a factory closing over the raw bytes of
CSVs uploaded in this turn, keyed by attachment id, so the model can only
ever operate on a file this user actually uploaded.
"""
from typing import Any

from . import csvs


def build(get_csv_bytes) -> dict:
    from .registry import Tool, _obj

    def _bytes(attachment_id: str) -> bytes:
        data = get_csv_bytes(attachment_id)
        if data is None:
            raise csvs.CsvError(
                "no uploaded CSV with that id in this conversation. Ask "
                "the user to attach one first.")
        return data

    def summarize_csv(attachment_id: str) -> dict[str, Any]:
        return csvs.summarize(_bytes(attachment_id))

    def filter_csv(attachment_id: str, column: str, op: str, value: str,
                   return_file: bool = False) -> dict[str, Any]:
        return csvs.filter_rows(_bytes(attachment_id), column, op, value,
                                "file" if return_file else "preview")

    def query_csv(attachment_id: str, column: str, op: str,
                 group_by: str = "") -> dict[str, Any]:
        return csvs.aggregate(_bytes(attachment_id), column, op, group_by)

    id_prop = {"attachment_id": {
        "type": "string",
        "description": "The id returned when the CSV was uploaded."}}

    return {t.name: t for t in [
        Tool("summarize_csv",
             "Get the column names, row count, and basic stats (min/max/"
             "mean for numeric columns, distinct value count for text "
             "columns) for an uploaded CSV, plus a preview of the first "
             "rows. Use this FIRST on any CSV question, before filtering "
             "or aggregating, since you need the real column names to do "
             "either correctly.",
             _obj(id_prop, ["attachment_id"]), summarize_csv),
        Tool("filter_csv",
             "Return rows from an uploaded CSV where one column matches a "
             "condition. Use exact column names from summarize_csv, not "
             "guesses. Set return_file to true only if the user wants the "
             "filtered rows as a downloadable file rather than just an "
             "answer.",
             _obj({**id_prop,
                   "column": {"type": "string"},
                   "op": {"type": "string",
                         "description": "==, !=, >, >=, <, <=, or contains"},
                   "value": {"type": "string"},
                   "return_file": {"type": "boolean"}},
                  ["attachment_id", "column", "op", "value"]),
             filter_csv),
        Tool("query_csv",
             "Compute sum, average, min, max, or count over one column of "
             "an uploaded CSV, optionally grouped by another column (e.g. "
             "total sales per region). Use exact column names from "
             "summarize_csv.",
             _obj({**id_prop,
                   "column": {"type": "string"},
                   "op": {"type": "string",
                         "description": "sum, avg, min, max, or count"},
                   "group_by": {"type": "string",
                               "description": "Optional column to group by."}},
                  ["attachment_id", "column", "op"]),
             query_csv),
    ]}
