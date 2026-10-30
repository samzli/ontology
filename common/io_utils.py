"""File I/O utilities shared across the Onti platform.

Pure parsers that read CSV / XLSX / XLS / JSON bytes into pandas frames
and expand workbooks into importable items. Depends only on ``pandas``
and :class:`common.exceptions.UploadError`; never talks to the database
or any :mod:`modules` business layer.
"""

from __future__ import annotations

import io
import json
from typing import Any, List, Optional

import pandas as pd

from common.exceptions import UploadError


def _read_csv(content: bytes) -> pd.DataFrame:
    """Decode CSV bytes by trying the supported encodings in turn.

    Args:
        content (bytes): Raw file bytes.

    Returns:
        pd.DataFrame: Frame read as all-string data without NA replacement.

    Raises:
        UploadError: When none of the candidate encodings can decode the file.
    """
    last_err: Optional[Exception] = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return pd.read_csv(
                io.BytesIO(content), encoding=enc, dtype=str, keep_default_na=False
            )
        except (UnicodeDecodeError, UnicodeError) as e:
            last_err = e
    raise UploadError(f"Could not decode CSV file: {last_err}")


def _excel_engine(filename: str) -> Optional[str]:
    """openpyxl for .xlsx; None lets pandas pick for the legacy .xls path.

    Args:
        filename (str): Workbook file name.

    Returns:
        Optional[str]: ``"openpyxl"`` for ``.xlsx``, otherwise ``None``.
    """
    return "openpyxl" if filename.rsplit(".", 1)[-1].lower() == "xlsx" else None


def list_sheets(filename: str, content: bytes) -> List[str]:
    """Sheet names of a workbook, in workbook order; empty for non-workbooks.

    Args:
        filename (str): File name, used to decide whether it is a workbook.
        content (bytes): Raw workbook bytes.

    Returns:
        List[str]: Sheet names in workbook order; empty for CSV / JSON files.

    Raises:
        UploadError: If the workbook cannot be opened.

    Example:
        >>> list_sheets("book.xlsx", raw_bytes)
        ['VNB Monthly', 'Agent Monthly']
    """
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in ("xlsx", "xls"):
        return []
    try:
        xls = pd.ExcelFile(io.BytesIO(content), engine=_excel_engine(filename))
        return [str(s) for s in xls.sheet_names]
    except Exception as e:
        raise UploadError(f"Failed to read sheets from '{filename}': {e}")


def _resolve_sheet(filename: str, content: bytes, sheet: Any) -> Any:
    """Validate the requested sheet and return a pandas ``sheet_name`` value.

    Accepts a sheet name or a zero-based index (as a string); defaults to the
    first sheet. Unknown sheets fail with the list of available names so the
    caller can retry — this is what powers the sheet picker in the UI.

    Args:
        filename (str): Workbook file name.
        content (bytes): Raw workbook bytes.
        sheet (Any): Requested sheet name or zero-based index; empty means the first.

    Returns:
        Any: A pandas ``sheet_name`` value (name, index, or ``0``).

    Raises:
        UploadError: If the requested sheet is not present in the workbook.
    """
    sheets = list_sheets(filename, content)
    if not sheets:
        return 0
    wanted = str(sheet).strip() if sheet not in (None, "") else ""
    if not wanted:
        return sheets[0]
    if wanted in sheets:
        return wanted
    if wanted.isdigit() and 0 <= int(wanted) < len(sheets):
        return int(wanted)
    raise UploadError(
        f"Sheet '{wanted}' not found in '{filename}'. "
        f"Available sheets: {', '.join(sheets)}"
    )


def parse_data_file(filename: str, content: bytes, sheet: Any = None) -> pd.DataFrame:
    """Parse a CSV/XLSX/XLS/JSON file. ``sheet`` selects a workbook tab (name or
    zero-based index); it defaults to the first sheet.

    Args:
        filename (str): File name; its extension selects the parser.
        content (bytes): Raw file bytes.
        sheet (Any): Optional workbook sheet name or index; ignored for CSV / JSON.

    Returns:
        pd.DataFrame: Parsed frame with stripped column names.

    Raises:
        UploadError: For unsupported extensions, unreadable sheets or parse failures.

    Example:
        >>> parse_data_file("sales.csv", raw_bytes).columns.tolist()
        ['month', 'ape']
    """
    ext = filename.rsplit(".", 1)[-1].lower()
    try:
        if ext == "csv":
            df = _read_csv(content)
        elif ext == "json":
            df = _read_json_records(content)
        elif ext in ("xlsx", "xls"):
            df = pd.read_excel(
                io.BytesIO(content),
                engine=_excel_engine(filename),
                dtype=str,
                keep_default_na=False,
                sheet_name=_resolve_sheet(filename, content, sheet),
            )
        else:
            raise UploadError(f"Unsupported data file type '.{ext}'.")
    except UploadError:
        raise
    except Exception as e:
        raise UploadError(f"Failed to parse '{filename}': {e}")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _basename(name: Any) -> str:
    """Strip any directory part (folder uploads send relative paths).

    Args:
        name (Any): File name or relative path.

    Returns:
        str: Basename with no directory component.
    """
    return str(name).replace("\\", "/").rsplit("/", 1)[-1].strip()


def _stem(filename: Any) -> str:
    """Strip the directory part and the last extension from a file name.

    Args:
        filename (Any): File name or relative path.

    Returns:
        str: Basename without its extension.
    """
    base = _basename(filename)
    return base.rsplit(".", 1)[0] if "." in base else base


def list_items(filename: str, content: bytes) -> List[str]:
    """Expand one uploaded file into its importable items.

    A workbook yields one item per sheet (in workbook order); CSV and JSON
    yield a single item named after the file. Keeping items separate is what
    lets one request carry many sheets or many files without mixing them up.

    Args:
        filename (str): Uploaded file name or relative path.
        content (bytes): Raw file bytes.

    Returns:
        List[str]: One item name per sheet for workbooks, else one file stem.

    Example:
        >>> list_items("book.xlsx", raw_bytes)
        ['VNB Monthly', 'Agent Monthly']
    """
    base = _basename(filename)
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if ext in ("xlsx", "xls"):
        return list_sheets(base, content)
    return [_stem(base)]


def _read_json_records(content: bytes) -> pd.DataFrame:
    """Parse a data-shaped JSON file into a flat table.

    Accepts a top-level array of objects, or an object carrying a
    ``records`` / ``data`` / ``rows`` array. An ontology-shaped document
    (one that contains ``entities``) is rejected with a pointer to the
    'Import pack' entry instead.

    Args:
        content (bytes): Raw JSON file bytes.

    Returns:
        pd.DataFrame: Flat frame of string cells with stripped column names.

    Raises:
        UploadError: For undecodable / invalid JSON, ontology documents, or
            payloads that are not a non-empty array of objects.
    """
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if text is None:
        raise UploadError("Could not decode JSON file.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise UploadError(f"Invalid JSON: {e}")
    if isinstance(data, dict) and ("entities" in data or "node_types" in data):
        raise UploadError(
            "This JSON is an ontology definition, not tabular data — import it "
            "through 'Import pack' (Ontology governance) instead."
        )
    records: Any = data
    if isinstance(data, dict):
        for k in ("records", "data", "rows"):
            if isinstance(data.get(k), list):
                records = data[k]
                break
        else:
            records = [data]
    if not isinstance(records, list) or not records:
        raise UploadError(
            "JSON must be a non-empty array of records, or an object with a "
            "'records'/'data'/'rows' array."
        )
    if not all(isinstance(r, dict) for r in records):
        raise UploadError("Every JSON record must be a key/value object.")
    df = pd.DataFrame(records).fillna("").astype(str)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def parse_item(filename: str, content: bytes, item: Any) -> pd.DataFrame:
    """Parse one item: a workbook sheet, or a whole CSV / JSON file.

    Args:
        filename (str): Uploaded file name or relative path.
        content (bytes): Raw file bytes.
        item (Any): Sheet name for workbooks; ignored for CSV / JSON.

    Returns:
        pd.DataFrame: Parsed frame for the item.

    Raises:
        UploadError: For unsupported types or unreadable content.

    Example:
        >>> parse_item("book.xlsx", raw_bytes, "VNB Monthly").shape
        (12, 11)
    """
    base = _basename(filename)
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if ext not in ("csv", "xlsx", "xls", "json"):
        raise UploadError(f"Unsupported data file type '.{ext}'.")
    return parse_data_file(base, content, item)
