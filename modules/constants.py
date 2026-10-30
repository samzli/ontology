"""Central constants shared by the HTTP layer.

Operational values that are not deployment configuration (truncation widths,
snippet sizes, UI limits) live here instead of being scattered as literals
through the routers. Values that *are* deployment-specific belong in
``config/config.json`` and are exposed through :mod:`modules.config`.
"""

from __future__ import annotations

# ---- Ask / history -------------------------------------------------------
MEMORY_ANSWER_CHARS = 600
AUDIT_QUESTION_CHARS = 500
AUDIT_ANSWER_CHARS = 800

# ---- Automatic analytics report -----------------------------------------
AUTO_REPORT_QUESTION_MIN = 6
AUTO_REPORT_QUESTION_TARGET = 8
SOURCE_SUMMARY_LIMIT = 4

# ---- Graph search snippets ----------------------------------------------
GRAPH_SNIPPET_ATTRS = 8
GRAPH_SNIPPET_RELATIONS = 6
GRAPH_SNIPPET_METRICS = 6
GRAPH_SNIPPET_EDGES = 6

# ---- Graph studio --------------------------------------------------------
STUDIO_MAX_COLUMNS = 25
STUDIO_MAX_QUESTIONS = 8
STUDIO_SAMPLE_CHARS = 400
STUDIO_SLUG_BASE = 40
STUDIO_SLUG_MAX = 49
STUDIO_RUN_TITLE_CHARS = 300
STUDIO_RUN_DETAIL_CHARS = 900

# ---- Report assembly limits ---------------------------------------------
REPORT_CORE_NODES = 12
REPORT_GRAPH_NODES = 40
REPORT_QA_ITEMS = 12
REPORT_QA_ANSWER_CHARS = 600
REPORT_METRIC_ROWS = 6
REPORT_SUMMARY_METRICS = 4
REPORT_CHART_NOTES = 2
REPORT_ATTR_LABELS = 10
REPORT_UID_CHARS = 12

# ---- Upload / import limits ---------------------------------------------
IMPORT_MAX_ERRORS = 50
IMPORT_PREVIEW_ROWS = 10
