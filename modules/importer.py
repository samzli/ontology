"""File ingestion for Onti.

- Data files (CSV / XLSX / XLS / JSON) uploaded against an instance table:
  parse (with an optional workbook-sheet pick) -> guess column mapping
  (optionally overridden by the user) -> type/required/enum validation ->
  preview (nothing is written) -> explicit commit creates a dataset + rows.
- Batch import: many files (or every sheet of a workbook, or a whole folder)
  in one request. Each file is expanded into items, each item is routed to a
  matching pack table or a brand-new one, then mapped, validated and
  committed together.
- Question lists (TXT / CSV / XLSX) parsed into question records and
  de-duplicated by the question bank's normalised key.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from common.exceptions import UploadError
from common.io_utils import (
    _basename,
    _stem,
    list_items,
    list_sheets,
    parse_data_file,
    parse_item,
)
from modules import admin_service as adm
from modules import ontology_service as svc
from modules.config import ALLOWED_UPLOAD_EXT, UPLOAD_MAX_MB
from modules.constants import IMPORT_MAX_ERRORS, IMPORT_PREVIEW_ROWS
from modules.db import get_conn

# ---------------------------------------------------------------------------
# Upload guards
# ---------------------------------------------------------------------------


def validate_upload(filename: str, size_bytes: int) -> str:
    """Validate an upload's file name and size against the configured guards.

    Args:
        filename: Client-supplied file name (a basename, no directories).
        size_bytes: Payload size in bytes.

    Returns:
        str: The normalised lowercase extension, e.g. ``"csv"``.

    Raises:
        UploadError: If the name is unsafe or has no extension or an
            unsupported one (400), or if the file exceeds ``UPLOAD_MAX_MB``
            (413).

    Example:
        >>> validate_upload("sales.csv", 1024)
        'csv'
    """
    max_mb = UPLOAD_MAX_MB
    allowed = {e.lower().lstrip(".") for e in ALLOWED_UPLOAD_EXT}
    # Basename only — defeats path traversal in stored filenames.
    if (
        "/" in filename
        or "\\" in filename
        or ".." in filename
        or filename != filename.strip()
    ):
        raise UploadError("Invalid filename: use a plain file name without paths.")
    if "." not in filename:
        raise UploadError("File must have an extension.")
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in allowed:
        raise UploadError(
            f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(allowed))}"
        )
    if size_bytes > max_mb * 1024 * 1024:
        raise UploadError(f"File too large: maximum {max_mb} MB.", 413)
    return ext


# ---------------------------------------------------------------------------
# Parsing  —  pure file I/O helpers live in :mod:`common.io_utils` (imported above)
# ---------------------------------------------------------------------------


def parse_question_file(filename: str, content: bytes) -> List[Dict[str, str]]:
    """Parse a TXT / CSV / XLSX question list into question records.

    TXT files yield one question per non-comment line; tabular files map a
    ``question``-like column (falling back to the first column) and an optional
    ``categ``-like column.

    Args:
        filename: File name; its extension selects the parser.
        content: Raw file bytes.

    Returns:
        List[Dict[str, str]]: Records with ``question`` and ``category`` keys.

    Raises:
        UploadError: For unsupported types, undecodable text or parse failures.

    Example:
        >>> parse_question_file("q.txt", b"What is VNB?\\n# skip me")
        [{'question': 'What is VNB?', 'category': ''}]
    """
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "txt":
        text = None
        for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
            try:
                text = content.decode(enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        if text is None:
            raise UploadError("Could not decode TXT file.")
        items = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                items.append({"question": line, "category": ""})
        return items
    if ext not in ("csv", "xlsx", "xls"):
        raise UploadError("Question lists must be TXT, CSV or XLSX.")
    df = parse_data_file(filename, content)
    qcol = next((c for c in df.columns if "question" in c.lower()), None)
    ccol = next((c for c in df.columns if "categ" in c.lower()), None)
    if qcol is None:
        qcol = df.columns[0]
    items = []
    for _, row in df.iterrows():
        q = str(row.get(qcol, "")).strip()
        if q:
            items.append(
                {
                    "question": q,
                    "category": str(row.get(ccol, "")).strip() if ccol else "",
                }
            )
    return items


# ---------------------------------------------------------------------------
# Mapping + validation
# ---------------------------------------------------------------------------


def _norm_header(h: str) -> str:
    """Lowercase a header and collapse non-alphanumeric runs to single spaces.

    Args:
        h: Raw header text.

    Returns:
        str: Normalised comparison key.
    """
    return re.sub(r"[^a-z0-9]+", " ", str(h).lower()).strip()


def guess_column_mapping(
    headers: List[str], columns_spec: List[Dict[str, Any]]
) -> Dict[str, str]:
    """Map uploaded header -> instance column key. Derived columns excluded.

    Exact (normalised) matches win; otherwise a containment match on key or
    label is used for headers of at least three characters.

    Args:
        headers: Headers as found in the uploaded file.
        columns_spec: Instance column specs (``key`` / ``label`` / ``expression``).

    Returns:
        Dict[str, str]: Uploaded header -> instance column key.

    Example:
        >>> guess_column_mapping(["Total APE"], [{"key": "total_ape"}])
        {'Total APE': 'total_ape'}
    """
    mapping: Dict[str, str] = {}
    targets = [c for c in columns_spec if not c.get("expression")]
    for h in headers:
        nh = _norm_header(h)
        best = None
        for c in targets:
            for cand in (c["key"], c.get("label", "")):
                nc = _norm_header(cand)
                if not nc:
                    continue
                if nh == nc:
                    best = c["key"]
                    break
            if best:
                break
        if not best:
            # Containment both directions, length>=3 to avoid noise.
            for c in targets:
                nc = _norm_header(c["key"])
                nl = _norm_header(c.get("label", ""))
                if len(nh) >= 3 and (
                    (nc and (nc in nh or nh in nc)) or (nl and (nl in nh or nh in nl))
                ):
                    best = c["key"]
                    break
        if best:
            mapping[h] = best
    return mapping


# Sentinel a header maps to when the user explicitly wants it ignored.
IGNORE_COLUMN = "__ignore__"


def parse_mapping(raw: str) -> Optional[Dict[str, str]]:
    """Parse the optional manual column-mapping JSON from the upload form.

    Returns ``None`` when absent/empty so the caller falls back to the guessed
    mapping; raises a 422 ``UploadError`` for malformed input.

    Args:
        raw: Raw JSON string from the upload form.

    Returns:
        Optional[Dict[str, str]]: Header -> column-key mapping, or ``None``.

    Raises:
        UploadError: With status 422 when the JSON is malformed or not an object.

    Example:
        >>> parse_mapping('{"Total APE": "total_ape"}')
        {'Total APE': 'total_ape'}
    """
    if not raw or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UploadError(f"Invalid column mapping: {e}", 422)
    if not isinstance(data, dict):
        raise UploadError("Invalid column mapping: expected a JSON object.", 422)
    return {str(k): str(v) for k, v in data.items()}


def apply_mapping_override(
    mapping: Dict[str, str],
    headers: List[str],
    columns_spec: List[Dict[str, Any]],
    override: Optional[Dict[str, str]],
) -> Dict[str, str]:
    """Layer a manual header→column mapping over the guessed one.

    An override value is an instance column key; an empty value or
    :data:`IGNORE_COLUMN` drops that header, and unknown keys are ignored.

    Args:
        mapping: Mapping guessed from the headers; updated in place.
        headers: Headers present in the uploaded file.
        columns_spec: Instance column specs; derived columns are not valid targets.
        override: Optional manual header -> column-key mapping.

    Returns:
        Dict[str, str]: The effective mapping (same object as ``mapping``).

    Example:
        >>> apply_mapping_override({}, ["Total APE"], specs,
        ...                        {"Total APE": "total_ape"})
        {'Total APE': 'total_ape'}
    """
    if not override:
        return mapping
    valid = {c["key"] for c in columns_spec if not c.get("expression")}
    for header, target in override.items():
        if header not in headers:
            continue
        t = str(target or "").strip()
        if t in ("", IGNORE_COLUMN) or t not in valid:
            mapping.pop(header, None)
        else:
            mapping[header] = t
    return mapping


def _coerce(raw: Any, spec: Dict[str, Any]) -> Any:
    """Convert one raw cell value to the column's declared data type.

    Blank / placeholder values become ``None``; numbers accept thousands
    separators, currency symbols, ``%`` and ``K``/``M``/``B`` suffixes.

    Args:
        raw: Raw cell value from the parsed file.
        spec: Column spec carrying ``data_type`` and optional ``fmt``.

    Returns:
        Any: ``None``, a stripped string, or an int / rounded float.

    Raises:
        ValueError: If the value is not numeric, or not integral for INTEGER.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "" or s.lower() in ("nan", "none", "null", "na", "n/a", "-"):
        return None
    dtype = spec.get("data_type", "STRING")
    if dtype == "STRING":
        return s
    cleaned = s.replace(",", "").replace("$", "").replace("￥", "").replace("¥", "")
    is_percent_literal = cleaned.endswith("%")
    cleaned = cleaned.rstrip("%").strip()
    cleaned = re.sub(r"(?i)\s*[mkb]\s*$", "", cleaned)
    try:
        v = float(cleaned)
    except ValueError:
        raise ValueError(f"'{s}' is not a number")
    if math.isnan(v):
        return None
    if is_percent_literal or spec.get("fmt") == "percent":
        if abs(v) > 1.5:  # 18 (%) -> 0.18 ; already-fraction values stay
            v = v / 100.0
    if dtype == "INTEGER":
        if abs(v - round(v)) > 1e-9:
            raise ValueError(f"'{s}' is not an integer")
        return int(round(v))
    return round(v, 6)


def validate_rows(
    df: pd.DataFrame, mapping: Dict[str, str], columns_spec: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Validate every data row against types, enums and required columns.

    Args:
        df: Parsed data frame (header row counted as row 1).
        mapping: Effective header -> column-key mapping.
        columns_spec: Instance column specs (type / enum_values / required).

    Returns:
        Dict[str, Any]: Report with totals, per-row errors (first 50), valid
        rows, a 10-row preview and mapping diagnostics.

    Example:
        >>> validate_rows(df, mapping, specs)["valid_count"]
        237
    """
    spec_by_key = {c["key"]: c for c in columns_spec}
    enum_by_key = {}
    for c in columns_spec:
        ev = c.get("enum_values") or ""
        vals = [v.strip() for v in re.split(r"[,;|]", ev) if v.strip()]
        if vals:
            enum_by_key[c["key"]] = set(vals)

    valid_rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for i, (_, raw_row) in enumerate(df.iterrows(), start=2):  # +2: header is row 1
        row: Dict[str, Any] = {}
        row_errors: List[str] = []
        for header, col_key in mapping.items():
            spec = spec_by_key[col_key]
            try:
                v = _coerce(raw_row.get(header), spec)
            except ValueError as e:
                row_errors.append(f"{spec.get('label') or col_key}: {e}")
                continue
            if (
                v is not None
                and col_key in enum_by_key
                and str(v) not in enum_by_key[col_key]
            ):
                row_errors.append(
                    f"{spec.get('label') or col_key}: '{v}' not in allowed values "
                    f"{sorted(enum_by_key[col_key])}"
                )
                continue
            if v is not None:
                row[col_key] = v
        for c in columns_spec:
            if c.get("required") and (
                c["key"] not in row or row[c["key"]] in (None, "")
            ):
                if c["key"] in mapping.values():
                    row_errors.append(
                        f"{c.get('label') or c['key']}: required value missing"
                    )
        if row_errors:
            errors.append({"row": i, "errors": row_errors})
        else:
            valid_rows.append(row)
    unmapped = [h for h in df.columns if h not in mapping]
    required_missing_cols = [
        c.get("label") or c["key"]
        for c in columns_spec
        if c.get("required") and c["key"] not in mapping.values()
    ]
    return {
        "total": int(len(df)),
        "valid_count": len(valid_rows),
        "error_count": len(errors),
        "errors": errors[:IMPORT_MAX_ERRORS],
        "rows": valid_rows,
        "preview": valid_rows[:IMPORT_PREVIEW_ROWS],
        "mapping": mapping,
        "unmapped_headers": unmapped,
        "missing_required_columns": required_missing_cols,
    }


def _table_spec(conn, table_id: int) -> Tuple[Any, List[Dict[str, Any]]]:
    """Target table row + its (non-derived) column specs, or 404.

    Args:
        conn: Open database connection.
        table_id: Instance table id to look up.

    Returns:
        Tuple[Any, List[Dict[str, Any]]]: The table row and its column specs.

    Raises:
        UploadError: With status 404 when the table does not exist.
    """
    tbl = conn.execute("SELECT * FROM pack_tables WHERE id=?", (table_id,)).fetchone()
    if tbl is None:
        raise UploadError("Target table not found.", 404)
    columns_spec = [
        dict(c)
        for c in conn.execute(
            "SELECT * FROM pack_columns WHERE table_id=? ORDER BY sort,id", (table_id,)
        )
    ]
    return tbl, columns_spec


def analyse_upload(
    conn,
    table_id: int,
    filename: str,
    content: bytes,
    sheet: Any = None,
    mapping_override: Optional[Dict[str, str]] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Parse + guess/layer the column mapping + validate. Writes nothing.

    This is the single pipeline shared by the preview and commit steps, so the
    two can never drift apart.

    Args:
        conn: Open database connection.
        table_id: Target instance table id.
        filename: Uploaded file name.
        content: Raw file bytes.
        sheet: Optional workbook sheet name or index.
        mapping_override: Optional manual header -> column-key mapping.

    Returns:
        Tuple[pd.DataFrame, Dict[str, Any]]: Parsed frame and the validation
        report enriched with table / sheet / column display metadata.

    Raises:
        UploadError: If the table is missing or the file cannot be parsed.

    Example:
        >>> df, report = analyse_upload(conn, 3, "sales.csv", raw_bytes)
        >>> report["valid_count"]
        120
    """
    tbl, columns_spec = _table_spec(conn, table_id)
    sheets = list_sheets(filename, content)
    df = parse_data_file(filename, content, sheet)
    headers = [str(c) for c in df.columns]
    mapping = guess_column_mapping(headers, columns_spec)
    mapping = apply_mapping_override(mapping, headers, columns_spec, mapping_override)
    report = validate_rows(df, mapping, columns_spec)
    report.update(
        {
            "table_id": table_id,
            "table_label": tbl["label"] or tbl["key"],
            "filename": filename,
            "sheet": (
                str(sheet) if sheet not in (None, "") else (sheets[0] if sheets else "")
            ),
            "sheets": sheets,
            "headers": headers,
            "instance_columns": [
                {
                    "key": c["key"],
                    "label": c.get("label") or c["key"],
                    "data_type": c.get("data_type", "STRING"),
                    "required": bool(c.get("required")),
                }
                for c in columns_spec
                if not c.get("expression")
            ],
        }
    )
    return df, report


def preview_upload(
    table_id: int,
    filename: str,
    content: bytes,
    sheet: Any = None,
    mapping_override: Optional[Dict[str, str]] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Full parse+validate pipeline for the dry-run step. Writes nothing.

    Args:
        table_id: Target instance table id.
        filename: Uploaded file name.
        content: Raw file bytes.
        sheet: Optional workbook sheet name or index.
        mapping_override: Optional manual header -> column-key mapping.

    Returns:
        Tuple[pd.DataFrame, Dict[str, Any]]: Parsed frame and validation report.

    Raises:
        UploadError: If the table is missing or the file cannot be parsed.

    Example:
        >>> df, report = preview_upload(3, "sales.csv", raw_bytes)
        >>> report["error_count"]
        0
    """
    with get_conn() as conn:
        return analyse_upload(
            conn, table_id, filename, content, sheet, mapping_override
        )


def commit_upload(
    conn,
    actor: str,
    table_id: int,
    dataset_name: str,
    filename: str,
    content: bytes,
    sheet: Any = None,
    mapping_override: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Atomic commit: a single bad row aborts the whole upload.

    Args:
        conn: Open database connection.
        actor: User or system identity recorded in the audit trail.
        table_id: Target instance table id.
        dataset_name: Dataset name; falls back to the file name.
        filename: Uploaded file name.
        content: Raw file bytes.
        sheet: Optional workbook sheet name or index.
        mapping_override: Optional manual header -> column-key mapping.

    Returns:
        Dict[str, Any]: Dataset id, imported row count, mapping, unmapped
        headers and the sheet used.

    Raises:
        UploadError: When any row is invalid, when the file yields no rows, or
            when the file cannot be parsed.

    Example:
        >>> commit_upload(conn, "alice", 3, "FY25 sales", "sales.csv", raw_bytes)
        {'dataset_id': 12, 'rows': 120, ...}
    """
    _, report = analyse_upload(
        conn, table_id, filename, content, sheet, mapping_override
    )
    if report["error_count"]:
        raise UploadError(
            f"{report['error_count']} invalid row(s); nothing was imported. "
            "Fix the file or adjust the mapping and try again."
        )
    if not report["valid_count"]:
        raise UploadError("The file contains no importable rows.")
    did = adm.create_dataset(
        conn,
        actor,
        table_id,
        dataset_name or filename,
        filename,
        report["rows"],
        kind="upload",
        activate=True,
    )
    return {
        "dataset_id": did,
        "rows": report["valid_count"],
        "mapping": report["mapping"],
        "unmapped_headers": report["unmapped_headers"],
        "sheet": report["sheet"],
    }


# ---------------------------------------------------------------------------
# Batch import: many files / many sheets in one request
# ---------------------------------------------------------------------------

# Target sentinel meaning "force a brand-new table for this item".
NEW_TARGET = "__new__"


# The batch I/O helpers (_basename, _stem, list_items, _read_json_records,
# parse_item) live in :mod:`common.io_utils` (imported at the top of this file).


def _column_type(values: List[Any]) -> str:
    """Library type for an inferred column: numeric -> INTEGER/FLOAT, else STRING."""
    seen = False
    numeric = True
    all_int = True
    for v in values:
        s = str(v).strip()
        if s == "" or s.lower() in ("nan", "none", "null", "na", "n/a", "-"):
            continue
        seen = True
        cleaned = (
            s.replace(",", "")
            .replace("$", "")
            .replace("￥", "")
            .replace("¥", "")
            .rstrip("%")
        )
        try:
            f = float(cleaned)
        except ValueError:
            numeric = False
            break
        if abs(f - round(f)) > 1e-9:
            all_int = False
    if seen and numeric:
        return "INTEGER" if all_int else "FLOAT"
    return "STRING"


def _infer_new_table(df: pd.DataFrame, label: str) -> Dict[str, Any]:
    """Propose a brand-new pack table structure from a parsed item."""
    columns: List[Dict[str, Any]] = []
    mapping: Dict[str, str] = {}
    used: set = set()
    for i, h in enumerate(df.columns):
        header = str(h)
        ckey = adm._slugify(header)
        base = ckey
        n = 2
        while ckey in used:
            ckey = f"{base}_{n}"
            n += 1
        used.add(ckey)
        col_label = svc._humanize(header).replace("_", " ").replace("-", " ").strip()
        columns.append(
            {
                "key": ckey,
                "label": col_label or header,
                "data_type": _column_type(list(df[h])),
                "required": False,
                "sort": i,
            }
        )
        mapping[header] = ckey
    return {
        "key": adm._slugify(_basename(label)),
        "label": _basename(label),
        "columns": columns,
        "mapping": mapping,
    }


def _plan_index(plan: Any) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Normalise a client plan (list of ``{file,item,target,mapping,skip}``,
    or its raw JSON string) into a lookup keyed by ``(basename, item)``.

    Args:
        plan: Parsed plan list or raw JSON string.

    Returns:
        Dict[Tuple[str, str], Dict[str, Any]]: Plan entries keyed by
        ``(file basename, item name)``.

    Raises:
        UploadError: With status 422 when the JSON string is malformed.
    """
    items = plan
    if isinstance(plan, str) and plan.strip():
        try:
            items = json.loads(plan)
        except json.JSONDecodeError as e:
            raise UploadError(f"Invalid batch plan: {e}", 422)
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if isinstance(items, list):
        for p in items:
            if isinstance(p, dict):
                index[(_basename(p.get("file", "")), str(p.get("item", "")))] = p
    return index


def _empty_item(file: str, item: str = "") -> Dict[str, Any]:
    """Build a blank per-item report used for skipped or unparsable items.

    Args:
        file: File name the item belongs to.
        item: Item (sheet) name; empty for a whole-file item.

    Returns:
        Dict[str, Any]: Report dict with all counters zeroed and lists empty.
    """
    return {
        "file": file,
        "item": item,
        "skip": False,
        "parse_error": "",
        "headers": [],
        "mapping": {},
        "unmapped_headers": [],
        "instance_columns": [],
        "missing_required_columns": [],
        "errors": [],
        "error_count": 0,
        "valid_count": 0,
        "target": {"key": "", "label": "", "is_new": False},
        "new_columns": [],
    }


def _analyse_one(
    conn,
    pack_id: int,
    filename: str,
    content: bytes,
    item: str,
    tables: List[Dict[str, Any]],
    overrides: Dict[Tuple[str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    """Route + map + validate a single item. Writes nothing.

    Args:
        conn: Open database connection.
        pack_id: Pack that owns the candidate tables.
        filename: Uploaded file name.
        content: Raw file bytes.
        item: Item (sheet) name being analysed.
        tables: Existing pack tables used for name-based routing.
        overrides: Plan entries keyed by ``(file, item)``.

    Returns:
        Dict[str, Any]: Per-item report (target, mapping, errors, valid rows).
    """
    ov = overrides.get((filename, item)) or overrides.get((filename, "")) or {}
    report = _empty_item(filename, item)
    report["skip"] = bool(ov.get("skip"))
    try:
        df = parse_item(filename, content, item)
    except UploadError as e:
        report["parse_error"] = e.message
        return report
    headers = [str(c) for c in df.columns]
    report["headers"] = headers

    target_key = str(ov.get("target") or "").strip()
    tbl = None
    if target_key and target_key != NEW_TARGET:
        tbl = next((t for t in tables if t["key"] == target_key), None)
    elif not target_key:
        tbl = adm.find_table_by_name(conn, pack_id, item) or adm.find_table_by_name(
            conn, pack_id, _stem(filename)
        )

    if tbl is not None:
        columns_spec = [
            dict(c)
            for c in conn.execute(
                "SELECT * FROM pack_columns WHERE table_id=? ORDER BY sort,id",
                (tbl["id"],),
            )
        ]
        report["target"] = {
            "key": tbl["key"],
            "label": tbl.get("label") or tbl["key"],
            "is_new": False,
            "table_id": tbl["id"],
        }
        mapping = guess_column_mapping(headers, columns_spec)
    else:
        inferred = _infer_new_table(df, item or _stem(filename))
        columns_spec = inferred["columns"]
        report["target"] = {
            "key": inferred["key"],
            "label": inferred["label"],
            "is_new": True,
        }
        report["new_columns"] = [dict(c) for c in columns_spec]
        mapping = dict(inferred["mapping"])

    mapping = apply_mapping_override(mapping, headers, columns_spec, ov.get("mapping"))
    report.update(validate_rows(df, mapping, columns_spec))
    report["instance_columns"] = [
        {
            "key": c["key"],
            "label": c.get("label") or c["key"],
            "data_type": c.get("data_type", "STRING"),
            "required": bool(c.get("required")),
        }
        for c in columns_spec
        if not c.get("expression")
    ]
    return report


def analyse_batch(
    conn,
    pack_id: int,
    files: List[Dict[str, Any]],
    plan: Any = None,
    with_rows: bool = False,
) -> Dict[str, Any]:
    """Dry-run of a batch upload. Writes nothing.

    Every file is expanded into items; each item is routed to an existing
    pack table by name (or marked as a table to be created), then mapped and
    validated. ``files`` is a list of ``{"filename": str, "content": bytes}``.

    Args:
        conn: Open database connection.
        pack_id: Pack that owns (or will own) the target tables.
        files: Files to analyse, each with ``filename`` and ``content``.
        plan: Optional client plan (list or raw JSON) routing items to targets.
        with_rows: When ``True``, keep validated rows in each item report.

    Returns:
        Dict[str, Any]: Existing tables, per-item reports and an ``ok`` flag.

    Example:
        >>> analyse_batch(conn, 1, [{"filename": "a.csv", "content": raw}])["ok"]
        True
    """
    tables = adm.list_tables(conn, pack_id)
    overrides = _plan_index(plan)
    reports: List[Dict[str, Any]] = []
    for f in files:
        filename = _basename(f.get("filename", ""))
        content = f.get("content") or b""
        try:
            validate_upload(filename, len(content))
            item_names = list_items(filename, content)
        except UploadError as e:
            item = _empty_item(filename)
            item["parse_error"] = e.message
            reports.append(item)
            continue
        for name in item_names:
            reports.append(
                _analyse_one(conn, pack_id, filename, content, name, tables, overrides)
            )
    if not with_rows:
        for it in reports:
            it.pop("rows", None)
            it.pop("total", None)
    return {
        "pack_id": pack_id,
        "tables": [
            {"id": t["id"], "key": t["key"], "label": t.get("label") or t["key"]}
            for t in tables
        ],
        "items": reports,
        "ok": all(
            i["skip"]
            or (not i["parse_error"] and not i["error_count"] and i["valid_count"])
            for i in reports
        ),
    }


def commit_batch(
    conn,
    actor: str,
    pack_id: int,
    files: List[Dict[str, Any]],
    plan: Any = None,
) -> Dict[str, Any]:
    """Commit a batch: every item is re-analysed on the server, then written.

    Any unparsable item, invalid row or empty item aborts the whole batch
    (nothing is written) — the same 'one bad row aborts all' contract the
    single-file upload uses. New tables get a matching ontology entity and
    attributes so the pack stays internally consistent.

    Args:
        conn: Open database connection.
        actor: User or system identity recorded in the audit trail.
        pack_id: Pack that owns (or will own) the target tables.
        files: Files to import, each with ``filename`` and ``content``.
        plan: Optional client plan (list or raw JSON) routing items to targets.

    Returns:
        Dict[str, Any]: Per-item results, created tables and the total row count.

    Raises:
        UploadError: If every item was skipped, or if any item could not be
            parsed, had invalid rows or had no rows; nothing is written then.

    Example:
        >>> commit_batch(conn, "system", 1, [{"filename": "a.csv",
        ...                                  "content": raw}])["rows_total"]
        120
    """
    analysis = analyse_batch(conn, pack_id, files, plan, with_rows=True)
    active = [i for i in analysis["items"] if not i["skip"]]
    if not active:
        raise UploadError("Nothing to import: every item was skipped.")
    bad = [
        i
        for i in active
        if i.get("parse_error") or i["error_count"] or not i["valid_count"]
    ]
    if bad:
        raise UploadError(
            f"{len(bad)} item(s) could not be imported; nothing was written. "
            "Fix the files or adjust the mapping and try again."
        )

    taken = {t["key"] for t in adm.list_tables(conn, pack_id)}
    created: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    rows_total = 0
    for it in active:
        target = it["target"]
        table_key = target["key"]
        if target["is_new"]:
            key = table_key
            base = key
            n = 2
            while key in taken:
                key = f"{base}_{n}"
                n += 1
            taken.add(key)
            label = target["label"] or key
            origin = f"Imported from {it['file']} / {it['item']}".strip(" /")
            adm.upsert_entity(
                conn,
                actor,
                pack_id,
                {"key": key, "label_en": label, "layer": 3, "description": origin},
            )
            table_id = adm.create_table(
                conn,
                actor,
                pack_id,
                key,
                label,
                key,
                origin,
                list(it.get("new_columns", [])),
            )
            for c in it.get("new_columns", []):
                adm.upsert_attribute(
                    conn,
                    actor,
                    pack_id,
                    {
                        "entity_key": key,
                        "key": c["key"],
                        "label_en": c.get("label") or c["key"],
                        "data_type": c.get("data_type", "STRING"),
                        "required": bool(c.get("required")),
                        "sort": c.get("sort", 0),
                    },
                )
            adm.sync_visual_graph(conn, actor, pack_id)
            table_key = key
            created.append({"key": key, "label": label, "table_id": table_id})
        else:
            table_id = target["table_id"]
        did = adm.create_dataset(
            conn,
            actor,
            table_id,
            it["item"] or it["file"],
            it["file"],
            it["rows"],
            kind="upload",
            activate=True,
        )
        rows_total += it["valid_count"]
        results.append(
            {
                "file": it["file"],
                "item": it["item"],
                "table_id": table_id,
                "table_key": table_key,
                "dataset_id": did,
                "rows": it["valid_count"],
            }
        )
    return {
        "items": results,
        "created_tables": created,
        "rows_total": rows_total,
    }
