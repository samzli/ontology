"""Generic OpenAI-compatible model client + model configuration center.

Any chat-completions endpoint that speaks the OpenAI protocol works
(OpenAI, Azure-compatible gateways, SiliconFlow, vLLM, Ollama-OpenAI,
Together, DeepSeek, ...). Configurations are stored in ``model_configs``
with the API key encrypted at rest; the platform never hard-codes a model
name — the administrator registers and selects the default model in the
admin portal (a legacy ``.env`` configuration is migrated once as a seed).
"""

from __future__ import annotations

import json
import sqlite3
import ssl
import time
from typing import Any, Dict, List, Optional

import httpx

from modules.db import audit, decrypt_secret, encrypt_secret, mask_secret, now

# ===========================================================================
# Prompt construction (generic; grounding text is built from the active pack)
# ===========================================================================

SYSTEM_PROMPT_TEMPLATE = """You are **Onti**, the ontology Q&A assistant for an insurance FP&A knowledge graph.
You answer business users' natural-language questions using ONLY the ontology knowledge base below.

Answering rules:
1. Use ONLY facts present in the knowledge base. Never invent numbers, entities, products, channels, agents or relationships.
2. If the knowledge base does not contain the answer, say so plainly and suggest a related question that is answerable.
3. Do all simple arithmetic from the given figures when needed (sums, products, percentages), and show the formula briefly.
4. Cite the ontology nodes/fields you used, and where relevant show the reasoning path (driver → component → KPI).
5. Respond in the SAME language as the user's question (Chinese question → Chinese answer; English → English),
   but keep technical identifiers (VNB, APE, node ids, edge ids…) in their original form.
6. Format with concise Markdown: short paragraphs, **bold** key figures, and Markdown tables for comparisons.
   Keep answers focused — usually under 250 words. Do not output a preamble.

==================== ONTOLOGY KNOWLEDGE BASE ====================
{ontology_context}
==================== END KNOWLEDGE BASE ====================
"""


def build_system_prompt(ontology_context: str) -> str:
    """Render the ontology-grounded answering prompt.

    Args:
        ontology_context (str): Pack text listing entities, nodes and rules.

    Returns:
        str: Full system prompt sent to the chat-completions endpoint.

    Example:
        >>> build_system_prompt("Entity: VNB").startswith("You are **Onti**")
        True
    """
    return SYSTEM_PROMPT_TEMPLATE.format(ontology_context=ontology_context)


# Used by the "without ontology" side of the Ask comparison. It carries the
# company's raw uploaded data (table-level column profiles, per-month
# aggregates, sample rows) but deliberately EXCLUDES the ontology layer —
# no entities, no relations, no formulas, no graph. This lets the model
# reason over the numbers without the pack's explicit structure, making the
# comparison fairer: the "with ontology" side adds rules + graph inference
# on top of the same data grounding.
BASELINE_SYSTEM_PROMPT_TEMPLATE = """You are an insurance business analyst with access to this company's **raw uploaded data**
but **NO access to any ontology layer** — no entities, no relations, no defined
metrics, no knowledge graph. Reason directly from the data below.

Answering rules:
1. Use the numbers you see in the data tables below — these are real company
   figures, not industry benchmarks.
2. Do NOT invent additional columns, entities or business rules; treat every
   field name literally.
3. If the data does not contain the exact month/segment the user asks for,
   say so explicitly rather than guessing.
4. Respond in the SAME language as the user's question.

==================== RAW UPLOADED DATA ====================
{table_profiles}
==================== END RAW DATA ====================
"""


def build_baseline_prompt(table_profiles: str) -> str:
    """Render the "no ontology" prompt around raw table profiles.

    Args:
        table_profiles (str): Column profiles, aggregates and sample rows of
            the pack's uploaded tables.

    Returns:
        str: System prompt used by the without-ontology comparison side.

    Example:
        >>> build_baseline_prompt("table: channel_monthly").startswith("You are an insurance")
        True
    """
    return BASELINE_SYSTEM_PROMPT_TEMPLATE.format(table_profiles=table_profiles)


# Used by the "with ontology" side of the Ask comparison when the deterministic
# rule engine and the deep-analysis layer have already computed every figure.
# The model is a NARRATOR here, not a calculator: it must reuse the supplied
# numbers verbatim and explain the reasoning chain, which is what makes the
# ontology answer auditable and reproducible.
SYNTHESIS_PROMPT_TEMPLATE = """You are **Onti**, an insurance FP&A reasoning assistant.
The ontology's rule engine and deterministic analysis layer have already computed
the facts below from the company's own tables. Your job is to EXPLAIN them.

Hard rules:
1. Every number must come from the ANALYSIS below. Do not recompute, round
   differently, or introduce any figure that is not present there.
2. Lead with the direct answer to the user's question, then walk through the
   reasoning chain in the same order as the analysis sections.
3. Name the tables and joins the ontology used (e.g. "channel_monthly joined to
   vnb_monthly", "mix joined to product on product_id") so the provenance is clear.
4. Keep the causal story complete: driver → factor → segment → metric. State
   which factor dominates and by how much.
5. Do NOT invent unnamed entities, products, agents or causes. If the analysis
   says something is not deductible, say so.
6. Respond in the SAME language as the user's question; keep identifiers
   (VNB, APE, table and column names) in their original form.
7. Concise Markdown with **bold** key figures. Under 400 words. No preamble.

==================== DETERMINISTIC ANALYSIS ====================
{analysis}
==================== END ANALYSIS ====================
{rule_block}
==================== ONTOLOGY KNOWLEDGE BASE ====================
{ontology_context}
==================== END KNOWLEDGE BASE ====================
"""

RULE_BLOCK_TEMPLATE = """
==================== RULE ENGINE RESULT ====================
{rules_answer}
==================== END RULE RESULT ====================
"""


def build_synthesis_prompt(
    analysis: str, ontology_context: str, rules_answer: str = ""
) -> str:
    """Render the narrator prompt that explains a pre-computed analysis.

    Args:
        analysis (str): Deterministic analysis text carrying every figure.
        ontology_context (str): Pack text listing entities, nodes and rules.
        rules_answer (str): Optional rule-engine answer appended verbatim;
            an empty string omits the rule block entirely.

    Returns:
        str: System prompt for the "rule engine + LLM" joint mode.

    Example:
        >>> build_synthesis_prompt("## VNB fell 4%", "Entity: VNB").startswith("You are **Onti**")
        True
    """
    rule_block = (
        RULE_BLOCK_TEMPLATE.format(rules_answer=rules_answer) if rules_answer else ""
    )
    return SYNTHESIS_PROMPT_TEMPLATE.format(
        analysis=analysis, ontology_context=ontology_context, rule_block=rule_block
    )


# ===========================================================================
# Configuration CRUD
# ===========================================================================


class ModelConfigError(Exception):
    """Validation or lookup failure in the model configuration centre.

    Attributes:
        message (str): Human-readable cause, safe to show in the admin UI.
        status_code (int): HTTP status the API layer should return.
    """

    def __init__(self, message: str, status_code: int = 400):
        """Build the exception with its UI message and HTTP status.

        Args:
            message (str): Human-readable cause shown to the administrator.
            status_code (int): HTTP status the API layer should return.

        Example:
            >>> ModelConfigError("Display name is required.", 422).status_code
            422
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ===========================================================================
# Credential normalisation + provider-error translation
# ===========================================================================
# Copy-pasting a key from a spreadsheet, an e-mail or a ``.env`` file routinely
# drags along quotation marks, a ``Bearer`` prefix or a trailing newline. Sent
# verbatim, those make the provider answer ``30014 Token is invalid`` even when
# the key itself is valid — which is exactly what a client hits when they paste
# a new key into the admin form. Every key entering the platform is therefore
# normalised once, at the boundary, and never stored dirty.

_PLACEHOLDER_API_KEYS = {
    "",
    "sk-",
    "sk-...",
    "sk-xxx",
    "sk-xxxx",
    "xxx",
    "api-key",
    "api_key",
    "changeme",
    "none",
    "null",
    "token",
    "your-api-key",
    "your_api_key",
    "<your-api-key>",
}

_QUOTE_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ("`", "`"),
    ("\u201c", "\u201d"),
    ("\u2018", "\u2019"),
)


def _strip_wrappers(value: Optional[str]) -> str:
    """Trim whitespace and peel nested wrapper quotes (straight/typographic).

    Args:
        value (Optional[str]): Raw pasted string, possibly quoted.

    Returns:
        str: The unwrapped value, ``""`` for ``None``.

    Example:
        >>> _strip_wrappers('"sk-abc"')
        'sk-abc'
    """
    out = (value or "").strip()
    changed = True
    while changed and out:
        changed = False
        for left, right in _QUOTE_PAIRS:
            if len(out) >= 2 and out.startswith(left) and out.endswith(right):
                out = out[1:-1].strip()
                changed = True
                break
    return out


def normalize_api_key(raw: Optional[str]) -> str:
    """Return the bare key with copy-paste artefacts removed.

    Surrounding whitespace, wrapper quotes (straight, back-tick, typographic)
    and a leading ``Bearer`` scheme are stripped. Known placeholder values
    (``sk-...``, ``your-api-key``, …) collapse to ``""`` so they are never
    stored or sent to a provider.

    Args:
        raw (Optional[str]): Key exactly as pasted by the administrator.

    Returns:
        str: Usable key, or ``""`` when the input is a known placeholder.

    Example:
        >>> normalize_api_key('Bearer "sk-live-123"')
        'sk-live-123'
    """
    key = _strip_wrappers(raw)
    # The scheme may sit outside or inside the quotes: ``Bearer "sk-…"``.
    while key[:7].lower() == "bearer ":
        key = _strip_wrappers(key[7:])
    if key.lower() in _PLACEHOLDER_API_KEYS:
        return ""
    return key


def normalize_model_id(raw: Optional[str]) -> str:
    """Return the bare model id with copy-paste wrappers removed.

    Gateways match model ids literally, so a trailing back-tick copied out of
    Markdown or a pair of surrounding quotes turns a valid id into a "model not
    found" answer. Case is deliberately preserved — ids are case-sensitive.

    Args:
        raw (Optional[str]): Model id exactly as pasted.

    Returns:
        str: Model id with surrounding quotes and whitespace removed.

    Example:
        >>> normalize_model_id("`gpt-4o-mini`")
        'gpt-4o-mini'
    """
    return _strip_wrappers(raw)


def normalize_base_url(raw: Optional[str]) -> str:
    """Trim whitespace, wrapper quotes and trailing slashes from an endpoint.

    A URL copied out of Markdown or a chat window often drags a stray
    back-tick along; that character is never valid in a URL, so both edges are
    cleaned before the trailing slash is dropped.

    Args:
        raw (Optional[str]): Base URL exactly as pasted.

    Returns:
        str: Normalised endpoint without a trailing ``/``.

    Example:
        >>> normalize_base_url("https://api.openai.com/v1/")
        'https://api.openai.com/v1'
    """
    return _strip_wrappers(raw).strip("`").strip().rstrip("/")


def describe_provider_error(
    status_code: Optional[int], body: str = ""
) -> Dict[str, Any]:
    """Turn a provider error body into an actionable message for the UI.

    Administrators are frequently non-technical, so echoing the provider's raw
    JSON (``{"code":30014,"data":null,"message":"Token is invalid."}``) tells
    them nothing. ``error`` carries the human-readable cause, ``hint`` the next
    step, and the untouched body stays in ``provider_error`` for support.

    Args:
        status_code (Optional[int]): HTTP status returned by the provider.
        body (str): Raw response body, possibly empty.

    Returns:
        Dict[str, Any]: ``error``, ``hint``, ``provider_error``,
        ``provider_code``, ``category`` and ``retryable``.

    Example:
        >>> describe_provider_error(401, '{"code":30014}')["category"]
        'auth'
    """
    raw = (body or "").strip()
    code: Optional[Any] = None
    message = ""
    try:
        obj = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, ValueError):
        obj = {}
    if isinstance(obj, dict):
        payload = obj.get("error")
        if not isinstance(payload, dict):
            payload = obj
        if isinstance(payload, dict):
            code = payload.get("code")
            message = str(payload.get("message") or payload.get("msg") or "")
    haystack = f"{message} {raw}".lower()
    auth_failed = (
        status_code in (401, 403)
        or code == 30014
        or "token is invalid" in haystack
        or "invalid token" in haystack
        or "invalid api key" in haystack
        or "unauthorized" in haystack
    )

    hint = ""
    # ``category`` / ``retryable`` separate a misconfigured model (a key or a
    # model id that will never work, retry pointless) from a transient blip
    # (rate limit, gateway restart). Anything that audits or cleans up stored
    # models needs that distinction — deleting a healthy model because one
    # test tripped over a timeout is exactly the damage it prevents.
    if auth_failed:
        category, retryable = "auth", False
        summary = "The provider rejected the API key"
        if code is not None and message:
            summary += f" (code {code} — {message})"
        elif code is not None:
            summary += f" (code {code})"
        elif message:
            summary += f" ({message})"
        hint = (
            "Paste the key exactly as issued — no surrounding quotes, no "
            "Bearer prefix, no line break or trailing space — then save and "
            "test again."
        )
    elif status_code == 404 or ("model" in haystack and "not found" in haystack):
        category, retryable = "model_not_found", False
        summary = "The provider could not find this model"
        hint = "Check the model id against the provider's catalogue."
    elif status_code == 429:
        category, retryable = "rate_limited", True
        summary = "The provider rate-limited this key"
        hint = "Wait a moment and retry, or use a key with a higher quota."
    elif status_code is not None and status_code >= 500:
        category, retryable = "provider_error", True
        summary = "The provider reported an internal error"
        hint = "Retry shortly; the provider's status page will confirm an outage."
    elif status_code is not None:
        category, retryable = "http_error", status_code in (408, 425)
        summary = f"The provider returned HTTP {status_code}"
        hint = "Check the base URL, the model id and the API key."
    else:
        category, retryable = "unreachable", True
        summary = "The provider could not be reached"
        hint = "Check the base URL, the network connection and any corporate proxy."

    if message and message not in summary:
        summary = f"{summary}: {message}"

    # When the provider returns an empty body (Nova 401, some on-prem
    # gateways), the raw field is useless — synthesise one from the
    # status + headers so support has *something* to work with.
    if not raw and status_code is not None:
        raw = f"(empty body, HTTP {status_code})"
    return {
        "error": summary,
        "hint": hint,
        "provider_error": raw[:300],
        "provider_code": code,
        "category": category,
        "retryable": retryable,
    }


def _exception_chain(exc: BaseException) -> List[str]:
    """Return the messages of ``exc`` and everything it was raised from.

    Args:
        exc (BaseException): Outer exception caught by the transport layer.

    Returns:
        List[str]: ``"TypeName: message"`` entries, outermost first.

    Example:
        >>> _exception_chain(ValueError("boom"))
        ['ValueError: boom']
    """
    texts: List[str] = []
    seen: set = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        texts.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    return texts


def describe_transport_error(exc: BaseException) -> Dict[str, str]:
    """Explain a connection-level failure in the operator's own terms.

    ``httpx.RequestError`` covers four very different failures — a self-signed
    certificate, a DNS typo, a timeout, and a gateway that drops the socket —
    and they need four different fixes. Reporting all of them as "could not be
    reached" is what sent operators hunting for network problems when the only
    thing wrong was the **Verify SSL** toggle. The real cause sits several
    exception layers down (``ssl.SSLCertVerificationError`` behind an
    ``httpx.ConnectError``), so the whole chain is inspected here.

    Args:
        exc (BaseException): Exception raised by ``httpx`` during a call.

    Returns:
        Dict[str, str]: ``error``, ``hint``, ``provider_error``, ``category``
        and ``retryable`` for the UI.

    Example:
        >>> describe_transport_error(httpx.ConnectError("refused"))["category"]
        'connection_refused'
    """
    chain = _exception_chain(exc)
    raw = " | ".join(chain)[:300]
    haystack = raw.lower()

    cert_failed = (
        isinstance(exc, ssl.SSLCertVerificationError)
        or "certificate_verify_failed" in haystack
        or "certificate verify failed" in haystack
        or "self-signed certificate" in haystack
        or "unable to get local issuer certificate" in haystack
        or "certificate is not trusted" in haystack
    )
    if cert_failed:
        return {
            "error": "The gateway's TLS certificate could not be verified",
            "hint": (
                "The endpoint most likely uses a self-signed certificate (common "
                "for internal gateways such as Deloitte Nova). Uncheck **Verify "
                "SSL certificate** for this model, then save and test again."
            ),
            "provider_error": raw,
            "category": "tls_certificate",
            "retryable": False,
        }

    timed_out = (
        isinstance(exc, httpx.TimeoutException)
        or isinstance(exc, TimeoutError)
        or "timed out" in haystack
        or "timeout" in haystack
    )
    if timed_out:
        return {
            "error": "The provider did not answer in time",
            "hint": (
                "The request timed out. Raise the model's **Timeout** setting, or "
                "check the gateway's health — a slow or queueing gateway answers "
                "eventually but never within a short test."
            ),
            "provider_error": raw,
            "category": "timeout",
            "retryable": True,
        }

    if isinstance(exc, ssl.SSLError) or "handshake failure" in haystack:
        return {
            "error": "The TLS handshake with the gateway failed",
            "hint": (
                "The endpoint speaks a TLS version or cipher this client cannot "
                "negotiate. Check the base URL (http:// vs https://) and whether "
                "the gateway expects an internal proxy."
            ),
            "provider_error": raw,
            "category": "tls_handshake",
            "retryable": False,
        }

    host_unknown = (
        "getaddrinfo" in haystack
        or "name or service not known" in haystack
        or "nodename nor servname" in haystack
        or "temporary failure in name resolution" in haystack
        or "no such host" in haystack
    )
    if host_unknown:
        return {
            "error": "The gateway's host name could not be resolved",
            "hint": (
                "DNS lookup failed. Check the base URL for typos (including stray "
                "characters) and make sure the server can reach this domain — an "
                "internal gateway often requires VPN or a hosts entry."
            ),
            "provider_error": raw,
            "category": "dns",
            "retryable": False,
        }

    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ProtocolError)) or (
        "server disconnected" in haystack or "incomplete" in haystack
    ):
        return {
            "error": "The provider closed the connection without a valid reply",
            "hint": (
                "The host answered but not with a chat-completions response. The "
                "base URL must be the full API prefix (for example "
                "https://host/v1, not the bare host), and the model id must exist "
                "on that gateway."
            ),
            "provider_error": raw,
            "category": "protocol",
            "retryable": False,
        }

    if "connection refused" in haystack or "connect call failed" in haystack:
        return {
            "error": "The gateway refused the connection",
            "hint": (
                "Nothing is listening on that host and port. Check the base URL "
                "scheme and port, and whether a corporate proxy blocks the call."
            ),
            "provider_error": raw,
            "category": "connection_refused",
            "retryable": False,
        }

    return {
        "error": "The provider could not be reached",
        "hint": "Check the base URL, the network connection and any corporate proxy.",
        "provider_error": raw,
        "category": "unreachable",
        "retryable": True,
    }


def _row_verify_ssl(row: sqlite3.Row) -> bool:
    """Read the optional ``verify_ssl`` column, defaulting to ``True``.

    Args:
        row (sqlite3.Row): A ``model_configs`` / ``user_model_configs`` row.

    Returns:
        bool: Stored flag, or ``True`` for legacy rows without the column.

    Example:
        >>> _row_verify_ssl(row)
        True
    """
    try:
        return bool(row["verify_ssl"])
    except (IndexError, KeyError):
        return True


def public_config(row: sqlite3.Row) -> Dict[str, Any]:
    """Shape a stored platform model row into the admin-facing dictionary.

    The API key is decrypted only to derive ``has_api_key`` and the masked
    preview; the plaintext never leaves this function.

    Args:
        row (sqlite3.Row): A ``model_configs`` row.

    Returns:
        Dict[str, Any]: Display fields, masked key and default/enabled flags.

    Example:
        >>> public_config(row)["has_api_key"]
        True
    """
    key = decrypt_secret(row["api_key_enc"])
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "model": row["model"],
        "temperature": row["temperature"],
        "max_tokens": row["max_tokens"],
        "timeout": row["timeout"],
        "verify_ssl": _row_verify_ssl(row),
        "enabled": bool(row["enabled"]),
        "is_default": bool(row["is_default"]),
        "has_api_key": bool(key),
        "api_key_masked": mask_secret(key),
        "created_at": row["created_at"],
    }


def list_configs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """List every platform model configuration, default first.

    Args:
        conn (sqlite3.Connection): Open database connection.

    Returns:
        List[Dict[str, Any]]: Public dictionaries ordered by ``is_default``
        then id.

    Example:
        >>> len(list_configs(conn))
        2
    """
    rows = conn.execute(
        "SELECT * FROM model_configs ORDER BY is_default DESC, id"
    ).fetchall()
    return [public_config(r) for r in rows]


def _validate_payload(name: str, base_url: str, model: str) -> None:
    """Validate the identity fields shared by the create and update paths.

    Args:
        name (str): Display name of the model.
        base_url (str): Normalised endpoint URL.
        model (str): Normalised model id.

    Raises:
        ModelConfigError: If a field is blank or the URL lacks http(s).

    Example:
        >>> _validate_payload("Nova", "https://nova/v1", "nova-1")
    """
    if not name or not name.strip():
        raise ModelConfigError("Display name is required.", 422)
    if not base_url or not (
        base_url.startswith("http://") or base_url.startswith("https://")
    ):
        raise ModelConfigError("Base URL must start with http:// or https://", 422)
    if not model or not model.strip():
        raise ModelConfigError("Model name is required.", 422)


def create_config(
    conn: sqlite3.Connection,
    actor: str,
    *,
    name: str,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    timeout: float = 90.0,
    verify_ssl: bool = True,
    enabled: bool = True,
) -> int:
    """Insert a platform model configuration and audit the action.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username performing the change, for the audit trail.
        name (str): Display name; must be unique.
        base_url (str): Endpoint URL, normalised before validation.
        api_key (str): Plaintext key, encrypted at rest.
        model (str): Model id, normalised before validation.
        temperature (float): Sampling temperature for answers.
        max_tokens (int): Default completion budget.
        timeout (float): Request timeout in seconds.
        verify_ssl (bool): Whether to verify the gateway certificate.
        enabled (bool): Whether the model may be used immediately.

    Returns:
        int: Primary key of the inserted row.

    Raises:
        ModelConfigError: If validation fails or the name already exists.

    Example:
        >>> create_config(conn, "admin", name="Nova", base_url="https://nova/v1",
        ...               api_key="sk-live", model="nova-1")
        3
    """
    # Normalise before validating: a pasted base URL carries spaces and a
    # trailing slash, and a pasted model id may carry quotes or a back-tick,
    # none of which must make an otherwise valid endpoint fail.
    name = (name or "").strip()
    base_url = normalize_base_url(base_url)
    model = normalize_model_id(model)
    _validate_payload(name, base_url, model)
    dup = conn.execute("SELECT 1 FROM model_configs WHERE name=?", (name,)).fetchone()
    if dup:
        raise ModelConfigError(f"Model '{name}' already exists.", 409)
    cur = conn.execute(
        "INSERT INTO model_configs(name, base_url, api_key_enc, model, temperature, "
        "max_tokens, timeout, verify_ssl, enabled, is_default, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,0,?)",
        (
            name,
            base_url,
            encrypt_secret(normalize_api_key(api_key)),
            model,
            float(temperature),
            int(max_tokens),
            float(timeout),
            1 if verify_ssl else 0,
            1 if enabled else 0,
            now(),
        ),
    )
    cid = int(cur.lastrowid)
    # First config becomes default automatically.
    if conn.execute("SELECT COUNT(*) n FROM model_configs").fetchone()["n"] == 1:
        conn.execute("UPDATE model_configs SET is_default=1 WHERE id=?", (cid,))
    audit(conn, actor, "model.create", f"id={cid} name={name}")
    return cid


def update_config(
    conn: sqlite3.Connection, actor: str, config_id: int, fields: Dict[str, Any]
) -> None:
    """Update one platform model configuration in place.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username performing the change.
        config_id (int): Id of the configuration to update.
        fields (Dict[str, Any]): Partial field map; an ``api_key`` equal to
            ``"__unchanged__"`` keeps the stored secret.

    Raises:
        ModelConfigError: If the row is missing, validation fails or the new
            name collides with another model.

    Example:
        >>> update_config(conn, "admin", 3, {"temperature": 0.2})
    """
    row = conn.execute(
        "SELECT * FROM model_configs WHERE id=?", (config_id,)
    ).fetchone()
    if row is None:
        raise ModelConfigError("Model configuration not found.", 404)
    name = (fields.get("name", row["name"]) or "").strip()
    base_url = normalize_base_url(fields.get("base_url", row["base_url"]))
    model = normalize_model_id(fields.get("model", row["model"]))
    _validate_payload(name, base_url, model)
    api_key = fields.get("api_key", "__unchanged__")
    # Three cases:
    #  1. "__unchanged__" — UI didn't touch the key, keep whatever's stored.
    #  2. "" / whitespace / placeholder (e.g. "sk-...") — deliberately clearing.
    #  3. Non-empty normalised string — replacing with a new key.
    if api_key == "__unchanged__":
        key_enc = row["api_key_enc"]
    else:
        key_enc = encrypt_secret(normalize_api_key(api_key))
    conn.execute(
        "UPDATE model_configs SET name=?, base_url=?, api_key_enc=?, model=?, "
        "temperature=?, max_tokens=?, timeout=?, verify_ssl=?, enabled=? WHERE id=?",
        (
            name,
            base_url,
            key_enc,
            model,
            float(fields.get("temperature", row["temperature"])),
            int(fields.get("max_tokens", row["max_tokens"])),
            float(fields.get("timeout", row["timeout"])),
            1 if bool(fields.get("verify_ssl", _row_verify_ssl(row))) else 0,
            1 if fields.get("enabled", bool(row["enabled"])) else 0,
            config_id,
        ),
    )
    audit(conn, actor, "model.update", f"id={config_id}")


def delete_config(conn: sqlite3.Connection, actor: str, config_id: int) -> None:
    """Delete a platform model configuration and promote a new default.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username performing the change.
        config_id (int): Id of the configuration to delete.

    Raises:
        ModelConfigError: If the configuration does not exist.

    Example:
        >>> delete_config(conn, "admin", 3)
    """
    row = conn.execute(
        "SELECT * FROM model_configs WHERE id=?", (config_id,)
    ).fetchone()
    if row is None:
        raise ModelConfigError("Model configuration not found.", 404)
    conn.execute("DELETE FROM model_configs WHERE id=?", (config_id,))
    # Promote another config if the default was deleted.
    if row["is_default"]:
        nxt = conn.execute(
            "SELECT id FROM model_configs WHERE enabled=1 ORDER BY id LIMIT 1"
        ).fetchone()
        if nxt:
            conn.execute(
                "UPDATE model_configs SET is_default=1 WHERE id=?", (nxt["id"],)
            )
    audit(conn, actor, "model.delete", f"id={config_id}")


def set_default(conn: sqlite3.Connection, actor: str, config_id: int) -> None:
    """Mark one configuration as the platform default.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username performing the change.
        config_id (int): Id of the configuration to promote.

    Raises:
        ModelConfigError: If the configuration does not exist.

    Example:
        >>> set_default(conn, "admin", 3)
    """
    row = conn.execute(
        "SELECT * FROM model_configs WHERE id=?", (config_id,)
    ).fetchone()
    if row is None:
        raise ModelConfigError("Model configuration not found.", 404)
    conn.execute("UPDATE model_configs SET is_default=0")
    conn.execute("UPDATE model_configs SET is_default=1 WHERE id=?", (config_id,))
    audit(conn, actor, "model.set_default", f"id={config_id}")


def get_default_client_config(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """Resolved default model: decrypted key + parameters. None if unavailable.

    Args:
        conn (sqlite3.Connection): Open database connection.

    Returns:
        Optional[Dict[str, Any]]: Row fields plus ``api_key``, or ``None``
        when no enabled configuration exists.

    Example:
        >>> get_default_client_config(conn)["model"]
        'gpt-4o-mini'
    """
    row = conn.execute(
        "SELECT * FROM model_configs WHERE is_default=1 AND enabled=1"
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM model_configs WHERE enabled=1 ORDER BY is_default DESC, id LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["api_key"] = decrypt_secret(row["api_key_enc"])
    return d


def get_client_config_by_id(
    conn: sqlite3.Connection, config_id: int
) -> Optional[Dict[str, Any]]:
    """Resolved config by id (decrypted key). Enabled rows only.

    Args:
        conn (sqlite3.Connection): Open database connection.
        config_id (int): Id of the configuration.

    Returns:
        Optional[Dict[str, Any]]: Row fields plus ``api_key``, or ``None``
        when the row is missing or disabled.

    Example:
        >>> get_client_config_by_id(conn, 3)["name"]
        'Nova'
    """
    row = conn.execute(
        "SELECT * FROM model_configs WHERE id=? AND enabled=1", (config_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["api_key"] = decrypt_secret(row["api_key_enc"])
    return d


# ---------------------------------------------------------------------------
# Per-user model configurations (the user-facing Model centre)
# ---------------------------------------------------------------------------


def public_user_config(row: sqlite3.Row) -> Dict[str, Any]:
    """Shape a per-user model row into the Model centre dictionary.

    Args:
        row (sqlite3.Row): A ``user_model_configs`` row.

    Returns:
        Dict[str, Any]: Display fields plus masked-key indicators.

    Example:
        >>> public_user_config(row)["has_api_key"]
        True
    """
    key = decrypt_secret(row["api_key_enc"])
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "model": row["model"],
        "temperature": row["temperature"],
        "max_tokens": row["max_tokens"],
        "timeout": row["timeout"],
        "verify_ssl": _row_verify_ssl(row),
        "has_api_key": bool(key),
        "api_key_masked": mask_secret(key),
        "created_at": row["created_at"],
    }


def list_user_configs(conn: sqlite3.Connection, user_id: int) -> List[Dict[str, Any]]:
    """List the model configurations owned by one user.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): Owner id.

    Returns:
        List[Dict[str, Any]]: Public dictionaries for that user's models.

    Example:
        >>> len(list_user_configs(conn, 7))
        1
    """
    rows = conn.execute(
        "SELECT * FROM user_model_configs WHERE user_id=? ORDER BY id", (user_id,)
    ).fetchall()
    return [public_user_config(r) for r in rows]


def create_user_config(
    conn: sqlite3.Connection,
    user_id: int,
    actor: str,
    *,
    name: str,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    timeout: float = 90.0,
    verify_ssl: bool = True,
    write_env: bool = True,
) -> int:
    """Register a model configuration owned by a user.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): Owner id.
        actor (str): Audit actor label.
        name (str): Display name, unique per user.
        base_url (str): Endpoint URL, normalised before validation.
        api_key (str): Plaintext key, encrypted at rest.
        model (str): Model id, normalised before validation.
        temperature (float): Sampling temperature.
        max_tokens (int): Default completion budget.
        timeout (float): Request timeout in seconds.
        verify_ssl (bool): Whether to verify the gateway certificate.
        write_env (bool): Mirror the model into ``.env`` when possible.

    Returns:
        int: Primary key of the inserted row.

    Raises:
        ModelConfigError: If validation fails or the user already owns that
            name.

    Example:
        >>> create_user_config(conn, 7, "alice", name="Nova",
        ...                    base_url="https://nova/v1", api_key="sk-live",
        ...                    model="nova-1")
        11
    """
    name = (name or "").strip()
    base_url = normalize_base_url(base_url)
    model = normalize_model_id(model)
    _validate_payload(name, base_url, model)
    dup = conn.execute(
        "SELECT 1 FROM user_model_configs WHERE user_id=? AND name=?",
        (user_id, name),
    ).fetchone()
    if dup:
        raise ModelConfigError(f"You already have a model named '{name}'.", 409)
    cur = conn.execute(
        "INSERT INTO user_model_configs(user_id, name, base_url, api_key_enc, model, "
        "temperature, max_tokens, timeout, verify_ssl, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            user_id,
            name,
            base_url,
            encrypt_secret(normalize_api_key(api_key)),
            model,
            float(temperature),
            int(max_tokens),
            float(timeout),
            1 if verify_ssl else 0,
            now(),
        ),
    )
    cid = int(cur.lastrowid)
    audit(conn, actor, "user_model.create", f"user={user_id} id={cid} name={name}")
    if write_env:
        try:
            append_env_model(
                name,
                base_url,
                normalize_api_key(api_key),
                model,
            )
        except OSError:
            pass  # .env is a convenience mirror only — DB stays the source of truth
    return cid


def update_user_config(
    conn: sqlite3.Connection, user_id: int, config_id: int, fields: Dict[str, Any]
) -> None:
    """Update a user-owned model configuration.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): Owner id, used to scope the update.
        config_id (int): Id of the configuration.
        fields (Dict[str, Any]): Partial field map; an ``api_key`` equal to
            ``"__unchanged__"`` keeps the stored secret.

    Raises:
        ModelConfigError: If the row is missing or validation fails.

    Example:
        >>> update_user_config(conn, 7, 11, {"timeout": 120})
    """
    row = conn.execute(
        "SELECT * FROM user_model_configs WHERE id=? AND user_id=?",
        (config_id, user_id),
    ).fetchone()
    if row is None:
        raise ModelConfigError("Model configuration not found.", 404)
    name = (fields.get("name", row["name"]) or "").strip()
    base_url = normalize_base_url(fields.get("base_url", row["base_url"]))
    model = normalize_model_id(fields.get("model", row["model"]))
    _validate_payload(name, base_url, model)
    api_key = fields.get("api_key", "__unchanged__")
    # Three cases — identical to update_config above.
    if api_key == "__unchanged__":
        key_enc = row["api_key_enc"]
    else:
        key_enc = encrypt_secret(normalize_api_key(api_key))
    conn.execute(
        "UPDATE user_model_configs SET name=?, base_url=?, api_key_enc=?, model=?, "
        "temperature=?, max_tokens=?, timeout=?, verify_ssl=? WHERE id=? AND user_id=?",
        (
            name,
            base_url,
            key_enc,
            model,
            float(fields.get("temperature", row["temperature"])),
            int(fields.get("max_tokens", row["max_tokens"])),
            float(fields.get("timeout", row["timeout"])),
            1 if bool(fields.get("verify_ssl", _row_verify_ssl(row))) else 0,
            config_id,
            user_id,
        ),
    )
    audit(conn, f"user:{user_id}", "user_model.update", f"id={config_id}")


def delete_user_config(conn: sqlite3.Connection, user_id: int, config_id: int) -> None:
    """Delete a user-owned model configuration.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): Owner id, used to scope the delete.
        config_id (int): Id of the configuration.

    Raises:
        ModelConfigError: If the row does not exist for that user.

    Example:
        >>> delete_user_config(conn, 7, 11)
    """
    row = conn.execute(
        "SELECT * FROM user_model_configs WHERE id=? AND user_id=?",
        (config_id, user_id),
    ).fetchone()
    if row is None:
        raise ModelConfigError("Model configuration not found.", 404)
    conn.execute(
        "DELETE FROM user_model_configs WHERE id=? AND user_id=?", (config_id, user_id)
    )
    audit(conn, f"user:{user_id}", "user_model.delete", f"id={config_id}")


def get_user_client_config_by_id(
    conn: sqlite3.Connection, user_id: int, config_id: int
) -> Optional[Dict[str, Any]]:
    """Resolve one user-owned configuration with its key decrypted.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): Owner id, used to scope the lookup.
        config_id (int): Id of the configuration.

    Returns:
        Optional[Dict[str, Any]]: Row fields plus ``api_key``, or ``None``
        when the row is missing for that user.

    Example:
        >>> get_user_client_config_by_id(conn, 7, 11)["model"]
        'nova-1'
    """
    row = conn.execute(
        "SELECT * FROM user_model_configs WHERE id=? AND user_id=?",
        (config_id, user_id),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["api_key"] = decrypt_secret(row["api_key_enc"])
    return d


def list_all_user_configs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Admin oversight view — every user-created model, key masked.

    Args:
        conn (sqlite3.Connection): Open database connection.

    Returns:
        List[Dict[str, Any]]: Public dictionaries plus the ``owner``
        username, newest first.

    Example:
        >>> list_all_user_configs(conn)[0]["owner"]
        'alice'
    """
    rows = conn.execute(
        "SELECT u.*, COALESCE(us.username, '') AS owner FROM user_model_configs u "
        "LEFT JOIN users us ON us.id = u.user_id ORDER BY u.id DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = public_user_config(r)
        d["owner"] = r["owner"]
        out.append(d)
    return out


def append_env_model(name: str, base_url: str, api_key: str, model: str) -> None:
    """Mirror a user-added model into ``.env`` so it can be reused outside the
    app (e.g. by scripts). The database remains the source of truth; this only
    writes/refreshes a clearly-labelled block.

    Args:
        name (str): Display name; becomes the block slug.
        base_url (str): Endpoint URL written to the block.
        api_key (str): Plaintext key written to the block.
        model (str): Model id written to the block.

    Raises:
        OSError: If ``.env`` cannot be read or written; callers such as
            ``create_user_config`` swallow it.

    Example:
        >>> append_env_model("Nova", "https://nova/v1", "sk-live", "nova-1")
    """
    import re as _re
    from pathlib import Path

    env_path = Path(__file__).resolve().parent.parent / ".env"
    slug = _re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "MODEL"
    begin, end = f"# >>> onti model: {slug} >>>", f"# <<< onti model: {slug} <<<"
    block = (
        f"{begin}\n"
        f"ONTI_MODEL_{slug}_NAME={name}\n"
        f"ONTI_MODEL_{slug}_BASE_URL={base_url}\n"
        f"ONTI_MODEL_{slug}_API_KEY={api_key}\n"
        f"ONTI_MODEL_{slug}_MODEL={model}\n"
        f"{end}\n"
    )
    text = ""
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
    # Replace an existing block for this slug, otherwise append.
    pattern = _re.compile(
        _re.escape(begin) + r".*?" + _re.escape(end) + r"\n?", _re.DOTALL
    )
    if pattern.search(text):
        text = pattern.sub(block, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n" + block
    env_path.write_text(text, encoding="utf-8")


def resolve_test_config(
    conn: sqlite3.Connection,
    *,
    config_id: Optional[int] = None,
    name: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    verify_ssl: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build the effective config for a connection test.

    With ``config_id`` the stored (decrypted) configuration is used; otherwise
    an ad-hoc configuration is assembled from the request fields. Raises
    :class:`ModelConfigError` when the config is missing or incomplete.

    Args:
        conn (sqlite3.Connection): Open database connection.
        config_id (Optional[int]): Stored configuration to reuse, if any.
        name (Optional[str]): Display name for an ad-hoc configuration.
        base_url (Optional[str]): Endpoint URL for an ad-hoc configuration.
        api_key (Optional[str]): Plaintext key for an ad-hoc configuration.
        model (Optional[str]): Model id for an ad-hoc configuration.
        verify_ssl (Optional[bool]): Override for the SSL toggle.

    Returns:
        Dict[str, Any]: Effective configuration handed to ``ConfiguredLLM``.

    Raises:
        ModelConfigError: If the stored row is missing, or ``base_url`` /
            ``model`` is absent for an ad-hoc configuration.

    Example:
        >>> resolve_test_config(conn, config_id=3)["name"]
        'Nova'
    """
    if config_id is not None:
        row = conn.execute(
            "SELECT * FROM model_configs WHERE id=?", (config_id,)
        ).fetchone()
        if row is None:
            raise ModelConfigError("Model not found.", 404)
        cfg = dict(row)
        cfg["api_key"] = normalize_api_key(decrypt_secret(row["api_key_enc"]))
        if verify_ssl is not None:
            cfg["verify_ssl"] = bool(verify_ssl)
        return cfg
    if not (base_url and model):
        raise ModelConfigError("base_url and model are required.", 422)
    return {
        "name": name or "test",
        "base_url": normalize_base_url(base_url),
        "api_key": normalize_api_key(api_key),
        "model": normalize_model_id(model),
        "verify_ssl": True if verify_ssl is None else bool(verify_ssl),
        "temperature": 0.1,
        "max_tokens": 16,
        "timeout": 30,
    }


def log_call(
    conn: sqlite3.Connection,
    *,
    user_id: Optional[int],
    config_id: Optional[int],
    model: str,
    success: bool,
    latency_ms: float,
    tokens: int = 0,
) -> None:
    """Record one model call in the ``llm_calls`` usage table.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (Optional[int]): Caller id, ``None`` for system calls.
        config_id (Optional[int]): Model configuration that was used.
        model (str): Model id reported by the call.
        success (bool): Whether the call completed successfully.
        latency_ms (float): Wall-clock duration in milliseconds.
        tokens (int): Token count, ``0`` when unknown.

    Example:
        >>> log_call(conn, user_id=7, config_id=3, model="nova-1",
        ...          success=True, latency_ms=812.5, tokens=190)
    """
    conn.execute(
        "INSERT INTO llm_calls(ts, user_id, model_config_id, model, success, "
        "latency_ms, tokens) VALUES(?,?,?,?,?,?,?)",
        (now(), user_id, config_id, model, 1 if success else 0, latency_ms, tokens),
    )


# ===========================================================================
# HTTP client (OpenAI Chat Completions, sync + SSE streaming)
# ===========================================================================


class ConfiguredLLM:
    """One configured OpenAI-compatible endpoint.

    Attributes:
        cfg (Dict[str, Any]): Raw configuration dictionary passed in.
        logger: Optional logger kept for diagnostics.
        name (str): Display name of the configuration.
        base_url (str): Normalised endpoint URL.
        api_key (str): Normalised API key, ``""`` when none is set.
        model_name (str): Model id sent to the provider.
        timeout (float): Read timeout in seconds.
        verify_ssl (bool): Whether TLS certificates are verified.
    """

    def __init__(self, cfg: Dict[str, Any], logger=None):
        """Normalise the configuration and build the request headers.

        Args:
            cfg (Dict[str, Any]): Resolved configuration with a decrypted
                ``api_key``.
            logger: Optional logger stored for later diagnostics.

        Example:
            >>> ConfiguredLLM({"name": "Nova", "base_url": "https://nova/v1",
            ...                "model": "nova-1", "api_key": "sk-live"}).model_name
            'nova-1'
        """
        self.cfg = cfg
        self.logger = logger
        self.name = cfg["name"]
        self.base_url = normalize_base_url(cfg["base_url"])
        # Last gate before the header is built: legacy rows, .env seeds and
        # ad-hoc test payloads all pass through here, so a quoted or padded key
        # can never reach the provider as an illegal / invalid token.
        self.api_key = normalize_api_key(cfg.get("api_key", ""))
        self.model_name = cfg["model"]
        self.timeout = float(cfg.get("timeout", 90))
        # Self-signed gateways (D.Nova, on-prem) need verify=False. We default
        # to True so the change is opt-in per-config.
        self.verify_ssl = bool(cfg.get("verify_ssl", True))
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._headers = headers

    @staticmethod
    def _parse_sse_block(body: str) -> Optional[Dict[str, Any]]:
        """Extract the first SSE event from a response body.

        Some gateways (e.g. D.Nova) unconditionally stream responses — even
        when the client set ``stream=False``. When they fail, they reply with
        ``event:error\\ndata:{...}`` wrapped in the same envelope. Return the
        parsed ``data`` object if one was found, else ``None``.

        Args:
            body (str): Raw response body.

        Returns:
            Optional[Dict[str, Any]]: First parsed ``data`` JSON object, or
            ``None`` when the body carries no usable event.

        Example:
            >>> ConfiguredLLM._parse_sse_block('data: {"object": "error"}')
            {'object': 'error'}
        """
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
        return None

    def _messages(
        self, system_prompt: str, question: str, history: Optional[List[Dict[str, str]]]
    ) -> List[Dict[str, str]]:
        """Build the chat-completions message list.

        Args:
            system_prompt (str): System message carrying the grounding.
            question (str): Current user question.
            history (Optional[List[Dict[str, str]]]): Prior turns; only the
                last six valid ones are kept.

        Returns:
            List[Dict[str, str]]: Messages ready for the request payload.

        Example:
            >>> client._messages("sys", "why?", None)[-1]["role"]
            'user'
        """
        msgs: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for turn in (history or [])[-6:]:
            role, content = turn.get("role"), (turn.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                msgs.append({"role": role, "content": content[:2000]})
        msgs.append({"role": "user", "content": question})
        return msgs

    # -- blocking ------------------------------------------------------------
    def answer(
        self,
        system_prompt: str,
        question: str,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send one blocking chat completion and normalise the result.

        Args:
            system_prompt (str): System message carrying the grounding.
            question (str): User question to answer.
            history (Optional[List[Dict[str, str]]]): Prior turns, oldest
                first.
            temperature (Optional[float]): Override for the configured
                sampling temperature.
            max_tokens (Optional[int]): Override for the configured budget.

        Returns:
            Dict[str, Any]: ``status`` of ``"success"`` or ``"error"`` plus
            ``response``, ``tokens_used``, ``latency_ms``, ``model`` on
            success, or ``error``, ``hint``, ``provider_error`` on failure.

        Raises:
            Exception: Unexpected failures (e.g. a malformed success body) are
                not swallowed; HTTP and transport errors become dictionaries.

        Example:
            >>> client.answer("sys", "why?")["status"]
            'success'
        """
        start = time.time()
        payload = {
            "model": self.model_name,
            "messages": self._messages(system_prompt, question, history),
            "temperature": (
                self.cfg.get("temperature", 0.3) if temperature is None else temperature
            ),
            "max_tokens": (
                self.cfg.get("max_tokens", 1024) if max_tokens is None else max_tokens
            ),
        }
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
            ) as client:
                resp = client.post("/chat/completions", json=payload)
                # Gateways like D.Nova always reply with text/event-stream
                # SSE envelopes, even for stream=False. Peel the error before
                # raising_for_status which would hide the body.
                sse = self._parse_sse_block(resp.text)
                if sse is not None and sse.get("object") == "error":
                    detail = describe_provider_error(resp.status_code, json.dumps(sse))
                    return {
                        "status": "error",
                        "error": detail["error"],
                        "hint": detail["hint"],
                        "provider_error": resp.text[:300],
                        "http_status": resp.status_code,
                        "latency_ms": round((time.time() - start) * 1000, 2),
                    }
                resp.raise_for_status()
                data = resp.json()
            return {
                "status": "success",
                "response": data["choices"][0]["message"]["content"],
                "tokens_used": data.get("usage", {}).get("total_tokens", 0),
                "latency_ms": round((time.time() - start) * 1000, 2),
                "model": self.model_name,
                "config_name": self.name,
            }
        except httpx.HTTPStatusError as e:
            detail = describe_provider_error(e.response.status_code, e.response.text)
            return {
                "status": "error",
                "error": detail["error"],
                "hint": detail["hint"],
                "provider_error": detail["provider_error"],
                "http_status": e.response.status_code,
                "latency_ms": round((time.time() - start) * 1000, 2),
            }
        except httpx.RequestError as e:
            # Distinguish SSL / DNS / timeout / dropped-socket failures: each
            # one has its own fix, and lumping them together as "not reached"
            # sent operators to the wrong place.
            detail = describe_transport_error(e)
            return {
                "status": "error",
                "error": detail["error"],
                "hint": detail["hint"],
                "provider_error": detail["provider_error"],
                "latency_ms": round((time.time() - start) * 1000, 2),
            }

    # -- streaming (yields SSE strings, same contract as v1) -----------------
    async def answer_stream(
        self,
        system_prompt: str,
        question: str,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """Stream one chat completion as SSE frames.

        Args:
            system_prompt (str): System message carrying the grounding.
            question (str): User question to answer.
            history (Optional[List[Dict[str, str]]]): Prior turns, oldest
                first.
            temperature (Optional[float]): Override for the configured
                sampling temperature.
            max_tokens (Optional[int]): Override for the configured budget.

        Yields:
            str: Complete ``data: {...}\\n\\n`` frames of type ``token``,
            ``error`` or ``done``.

        Raises:
            Exception: Unexpected failures are not swallowed; HTTP and
                transport errors are converted into error frames.

        Example:
            >>> async for frame in client.answer_stream("sys", "why?"):
            ...     print(frame.startswith("data: "))
            True
        """
        start = time.time()
        payload = {
            "model": self.model_name,
            "messages": self._messages(system_prompt, question, history),
            "temperature": (
                self.cfg.get("temperature", 0.3) if temperature is None else temperature
            ),
            "max_tokens": (
                self.cfg.get("max_tokens", 1024) if max_tokens is None else max_tokens
            ),
            "stream": True,
            # OpenAI-compatible providers return final usage when this is
            # set, enabling per-answer token accounting.
            "stream_options": {"include_usage": True},
        }
        full = ""
        tokens = 0
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
            ) as client:
                async with client.stream(
                    "POST", "/chat/completions", json=payload
                ) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                        detail = describe_provider_error(resp.status_code, body)
                        yield "data: " + json.dumps(
                            {
                                "type": "error",
                                "error": detail["error"],
                                "hint": detail["hint"],
                                "provider_error": detail["provider_error"],
                                "http_status": resp.status_code,
                            },
                            ensure_ascii=False,
                        ) + "\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        chunk = line[6:]
                        if chunk.strip() == "[DONE]":
                            break
                        try:
                            obj = json.loads(chunk)
                            content = (
                                obj.get("choices", [{}])[0]
                                .get("delta", {})
                                .get("content", "")
                            )
                            usage = obj.get("usage") or {}
                            if usage:
                                tokens = int(
                                    usage.get("total_tokens")
                                    or (
                                        int(usage.get("prompt_tokens") or 0)
                                        + int(usage.get("completion_tokens") or 0)
                                    )
                                )
                        except (
                            json.JSONDecodeError,
                            IndexError,
                            AttributeError,
                            ValueError,
                            TypeError,
                        ):
                            continue
                        if content:
                            full += content
                            yield f"data: {json.dumps({'type': 'token', 'content': content}, ensure_ascii=False)}\n\n"
        except httpx.RequestError as e:
            # Distinguish SSL / DNS / timeout / dropped-socket failures: each
            # one has its own fix, and lumping them together as "not reached"
            # sent operators to the wrong place.
            detail = describe_transport_error(e)
            yield "data: " + json.dumps(
                {
                    "type": "error",
                    "error": detail["error"],
                    "hint": detail["hint"],
                    "provider_error": detail["provider_error"],
                },
                ensure_ascii=False,
            ) + "\n\n"
            return
        if not tokens and full:
            # Provider did not return usage — fall back to a word-count
            # estimate so the admin still sees approximate consumption.
            tokens = max(1, len(full.split()))
        yield f"data: {json.dumps({'type': 'done', 'status': 'success', 'response': full, 'latency_ms': round((time.time() - start) * 100, 2), 'model': self.model_name, 'config_name': self.name, 'tokens': tokens}, ensure_ascii=False)}\n\n"

    # -- connection test ------------------------------------------------------
    async def test_connection(self) -> Dict[str, Any]:
        """Probe the endpoint with a five-token ping.

        Returns:
            Dict[str, Any]: ``status`` of ``"connected"`` or ``"error"`` plus
            ``model``, ``endpoint``, ``latency_ms`` on success, or ``error``,
            ``hint``, ``provider_error``, ``category``, ``retryable`` on
            failure.

        Raises:
            Exception: Unexpected failures are not swallowed; transport errors
                are converted into dictionaries.

        Example:
            >>> await client.test_connection()["status"]
            'connected'
        """
        start = time.time()
        try:
            # The probe honours the model's configured **Timeout**: capping it
            # at 30 s turned a busy-but-healthy gateway (a 5-token ping can
            # legitimately queue for over a minute at peak) into a false
            # "did not answer in time". Connecting is still capped at 10 s so
            # a dead host or a wrong port fails fast instead of waiting out
            # the read budget.
            timeout = httpx.Timeout(self.timeout, connect=min(10.0, self.timeout))
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._headers,
                timeout=timeout,
                verify=self.verify_ssl,
            ) as client:
                resp = await client.post(
                    "/chat/completions",
                    json={
                        "model": self.model_name,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 5,
                    },
                )
                latency = round((time.time() - start) * 1000, 2)
                # SSE-wrapped error: Nova and similar gateways always stream
                # and surface failures as event:error with object:"error".
                if resp.status_code == 200:
                    sse = self._parse_sse_block(resp.text)
                    if sse is not None and sse.get("object") == "error":
                        detail = describe_provider_error(
                            resp.status_code, json.dumps(sse)
                        )
                        detail.update(
                            {
                                "status": "error",
                                "http_status": resp.status_code,
                                "provider_error": resp.text[:300],
                                "latency_ms": latency,
                            }
                        )
                        return detail
                    return {
                        "status": "connected",
                        "model": self.model_name,
                        "endpoint": self.base_url,
                        "latency_ms": latency,
                    }
                detail = describe_provider_error(resp.status_code, resp.text)
                if not self.api_key and resp.status_code in (401, 403):
                    detail["error"] = (
                        "No API key was sent and the provider requires one"
                    )
                    detail["hint"] = (
                        "Enter the provider API key in the form, save it, then test again."
                    )
                return {
                    "status": "error",
                    "http_status": resp.status_code,
                    "error": detail["error"],
                    "hint": detail["hint"],
                    "provider_error": detail["provider_error"],
                    "provider_code": detail["provider_code"],
                    "category": detail["category"],
                    "retryable": detail["retryable"],
                    "latency_ms": latency,
                }
        except httpx.RequestError as e:
            # Distinguish SSL / DNS / timeout / dropped-socket failures: each
            # one has its own fix, and lumping them together as "not reached"
            # sent operators to the wrong place.
            detail = describe_transport_error(e)
            return {
                "status": "error",
                "error": detail["error"],
                "hint": detail["hint"],
                "provider_error": detail["provider_error"],
                "category": detail["category"],
                "retryable": detail["retryable"],
                "latency_ms": round((time.time() - start) * 1000, 2),
            }
