"""
extractor.py — one gpt-4o-mini call turns a filing's text into FilingSignals.

The prompt carries the domain knowledge that makes the difference between a
usable number and a plausible-looking wrong one on Indian exchange filings:

  * the units line ("Rs. in Lakhs") sits in the table header, not next to the
    figures, and changes every number by 100x if missed;
  * the year-ago column is the "corresponding quarter of the previous year",
    which is the THIRD numeric column, not the second (that is the preceding
    quarter — the QoQ comparative, which this formula does not use);
  * consolidated and standalone statements appear in the same PDF and disagree;
  * "order book of Rs 12,000 crore" is a stock, not a new win, and must not be
    scored as one.

The model is asked only to READ. Every derived figure — growth rates, order
totals, the score — is computed in Python afterwards, because that arithmetic
has to be reproducible and auditable.
"""
import json
import re

from langchain_core.prompts import ChatPromptTemplate

import config
from pdf_text import consolidated_first
from signals import FilingSignals

SYSTEM_PROMPT = """You are a financial analyst who reads Indian stock exchange \
(NSE/BSE) filings and extracts figures exactly as printed. You never estimate, \
infer, or fill gaps from memory. If a figure is not in the document, you return \
null for it.

UNITS — read them, do NOT convert them:
- Statements carry a units line in the header, e.g. "(Rs. in Lakhs)", \
"(Rs. in Millions)", "(Rs. in Crores)". Find it before reading any number and \
report it in `statement_unit`, and in each figure's `unit`.
- Put the number EXACTLY as printed in `raw_value`, with thousands separators \
removed and NOTHING else changed. "45,231.00" is 45231.00 — not 452.31, not \
45.231. Conversion to crore happens downstream and is not your job; doing it \
yourself introduces errors that are invisible in the output.
- Always put the figure as printed, with its unit, in `raw`.

PERIOD COLUMNS — a quarterly results table typically has these columns:
    | Quarter ended (current) | Preceding quarter | Corresponding quarter of \
previous year | Year to date | Previous year ended |
- `current`   = the CURRENT quarter ended column (the first one).
- `year_ago`  = the CORRESPONDING QUARTER OF THE PREVIOUS YEAR column.
- Do NOT use the preceding quarter as `year_ago`. That is the sequential \
comparative and it is the wrong basis here.

CONSOLIDATED vs STANDALONE:
- If the filing has BOTH, read the CONSOLIDATED (group) statement and set \
basis to "consolidated".
- If it has only a standalone statement, use that and set basis to "standalone".

METRICS:
- `revenue` = "Revenue from Operations". Do not substitute "Total Income" \
(which adds other income) unless Revenue from Operations is genuinely absent.
- `profit`  = Profit After Tax / Net Profit for the period, AFTER tax and after \
minority interest where shown. Not Profit Before Tax.
- Losses are NEGATIVE numbers. A figure in brackets like (45.20) is -45.20.

ORDERS — only NEW wins count:
- Include: orders/contracts/Letters of Award/Letters of Intent/work orders/ \
purchase orders that THIS filing announces the company has received.
- Exclude: total order book or backlog figures, pipeline or bid values, orders \
described as previously announced, and orders where the company is the BUYER \
rather than the recipient.
- If an order is announced but its value is withheld, still include it with \
value_cr null.
- Order values are often stated with GST included or excluded — take the number \
as printed and note the caveat in `notes` if it matters.
- ALWAYS set `status`. A filing that TERMINATES or CANCELS an order describes \
that order in full and states its value, because its whole purpose is to say \
what is being taken away — so the order looks exactly like a win. Read the \
SUBJECT line: "Termination of work order" is status TERMINATED, however \
favourably the work itself is described further down. Losing a contract is the \
opposite of winning one, and reporting it as a win is worse than missing it.

document_type:
- RESULTS   - quarterly or annual financial results
- ORDER_WIN - announcement of a new order/contract
- BOTH      - genuinely both in one document
- OTHER     - anything else

Return ONLY the structured object. Every quote in `evidence` must appear \
verbatim in the document."""

USER_PROMPT = """Company symbol: {symbol}
Announcement title: {title}

--- FILING TEXT ---
{pdf_text}
--- END FILING TEXT ---

Extract the structured signals."""


_llm = None
_chain = None


def _build_chain():
    """Build (and memoise) the extraction chain."""
    global _llm, _chain
    if _chain is not None:
        return _chain

    from langchain_openai import ChatOpenAI

    if not config.OPENAI_API_KEY:
        raise EnvironmentError(
            "OPENAI_API_KEY is not set. Add it to engine/.env "
            "(see .env.example) before starting the agent."
        )

    _llm = ChatOpenAI(
        model=config.OPENAI_MODEL,
        api_key=config.OPENAI_API_KEY,
        temperature=0,
        timeout=config.LLM_TIMEOUT_SEC,
        max_retries=config.LLM_MAX_RETRIES,
    )

    prompt = ChatPromptTemplate.from_messages(
        [("system", SYSTEM_PROMPT), ("human", USER_PROMPT)]
    )

    # Structured output via TOOL CALLING, pinned explicitly.
    #
    # The schema is still enforced by the API, so we never have to defend
    # against the model wrapping JSON in prose. The reason for naming the method
    # rather than taking the default: newer langchain-openai defaults to the
    # `json_schema` path, which calls `client.chat.completions.parse` — a method
    # that only exists on recent openai SDKs. This project shares site-packages
    # with shares/bot, so silently requiring an openai upgrade here would risk
    # that service. Tool calling is supported by every version of both, and by
    # gpt-4o-mini.
    _chain = prompt | _llm.with_structured_output(
        FilingSignals, method="function_calling"
    )
    return _chain


def _prepare_text(text: str) -> str:
    """Trim whitespace noise, put the consolidated statement first, then cap."""
    text = re.sub(r"[ \t]{3,}", "  ", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return consolidated_first(text)[: config.MAX_PDF_CHARS]


def extract_signals(text: str, symbol: str = "", title: str = "") -> FilingSignals:
    """
    Read one filing into FilingSignals.

    Raises on an unrecoverable model or network error so the caller can mark the
    filing FAILED and retry it on a later cycle — a transient 429 must not be
    recorded as "this filing has no signals".
    """
    chain = _build_chain()
    result = chain.invoke({
        "symbol": symbol or "",
        "title": (title or "")[:300],
        "pdf_text": _prepare_text(text),
    })

    # with_structured_output returns the pydantic object, but some langchain
    # versions hand back a dict for the same call — accept either.
    if isinstance(result, dict):
        return FilingSignals(**result)
    return result


def signals_to_json(signals: FilingSignals) -> dict:
    """Plain dict for the JSONB column, across pydantic v1 and v2."""
    if hasattr(signals, "model_dump"):
        return json.loads(signals.model_dump_json())
    return json.loads(signals.json())
