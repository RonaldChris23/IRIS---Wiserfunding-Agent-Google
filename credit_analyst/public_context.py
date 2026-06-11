"""Lane B: live public context about a company, sourced from Google via Gemini grounding.

Walled off from Wiserfunding's proprietary assessment (Lane A):
  * Runs on the USER'S OWN Gemini key (bound into the keyed runner). No extra cost, no key stored.
  * Returns ONLY web-grounded, cited facts. No credit/risk/lending opinion (that is Lane A's job).
  * Query-aware: answers the analyst's actual question about the company (ESG, ownership, news,
    results...), not just a fixed profile.
  * GUARDRAILED: it only does legitimate research about THE COMPANY. If the request is off-topic
    (general knowledge, coding, personal, abusive, or trying to use Iris as a general web search),
    the grounding model returns OUT_OF_SCOPE and we refuse to show public context. This is the
    "checker" that stops people asking it random things.
  * Best-effort + resilient: a normalised cache key, one retry, a 20s ceiling. Any failure returns
    available=false and NEVER raises. Lane A is never affected.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Callable

GROUNDING_MODEL = os.environ.get("WF_GROUNDING_MODEL", "gemini-2.5-flash")
_TTL = float(os.environ.get("WF_GROUNDING_TTL", "21600"))          # cache 6h
_TIMEOUT_MS = int(os.environ.get("WF_GROUNDING_TIMEOUT_MS", "45000"))  # headroom: grounding can be slow under load
_RETRIES = int(os.environ.get("WF_GROUNDING_RETRIES", "3"))            # don't let one transient hiccup fail the lane
_CACHE_MAX = 256
_OUT_OF_SCOPE = "OUT_OF_SCOPE"

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

_SUFFIXES = re.compile(r"\b(limited|ltd|plc|inc|incorporated|corp|corporation|llc|llp|gmbh|sa|ag|nv|bv|co)\b\.?",
                       re.IGNORECASE)


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = _SUFFIXES.sub(" ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _cache_get(key: str):
    with _lock:
        hit = _cache.get(key)
        if hit and (time.monotonic() - hit[0]) < _TTL:
            return hit[1]
        if hit:
            _cache.pop(key, None)
    return None


def _cache_put(key: str, value: dict) -> None:
    with _lock:
        if len(_cache) >= _CACHE_MAX and key not in _cache:
            oldest = min(_cache.items(), key=lambda it: it[1][0])[0]
            _cache.pop(oldest, None)
        _cache[key] = (time.monotonic(), value)


def _build_prompt(company: str, where: str, question: str) -> str:
    if question.strip():
        ask = (f'The analyst asks, about this company specifically: "{question.strip()}".\n'
               "Answer THAT question, briefly and factually, using Google Search.")
    else:
        ask = ("Give a brief factual profile: (1) what the company does in one or two sentences; "
               "(2) up to four recent, notable, verifiable developments from roughly the last 18 "
               "months (funding, M&A, leadership, results, regulatory, major contracts).")
    return (
        "You gather PUBLIC, web-sourced background about a company for a credit analyst, using "
        "Google Search. You are NOT a general assistant.\n\n"
        f"Company: {company}{where}\n{ask}\n\n"
        "Rules:\n"
        "- Use ONLY what Google Search supports, and ONLY about THIS company. Stay factual.\n"
        "- Stay strictly on legitimate company research: business, products, financials, results, "
        "funding, ownership, leadership, news, ESG, legal/regulatory, market position.\n"
        f"- If the request is NOT legitimate research about this company (general knowledge, "
        f"coding, personal, abusive, or using you as a general web search), reply with EXACTLY "
        f"{_OUT_OF_SCOPE} and nothing else.\n"
        "- Give NO credit, lending, investment or risk opinion. Facts, not judgements. If little "
        "is found, say so plainly.\n"
        "- Under ~150 words, short bullet points."
    )


def _ground_once(api_key: str, prompt: str) -> tuple[str, list, list]:
    """One grounded call. Returns (text, sources, queries). Raises on transport/API error."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=_TIMEOUT_MS))
    resp = client.models.generate_content(
        model=GROUNDING_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.2,
        ),
    )
    text = (getattr(resp, "text", "") or "").strip()
    sources: list[dict] = []
    queries: list[str] = []
    try:
        gm = resp.candidates[0].grounding_metadata
        for q in (getattr(gm, "web_search_queries", None) or []):
            q = (q or "").strip()
            if q and q not in queries:
                queries.append(q)
        for ch in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(ch, "web", None)
            if not web:
                continue
            uri = (getattr(web, "uri", "") or "").strip()
            title = (getattr(web, "title", "") or "").strip()
            domain = (getattr(web, "domain", "") or "").strip()
            if uri and not any(s["url"] == uri for s in sources):
                sources.append({"title": title[:120], "url": uri, "domain": domain[:80]})
    except Exception:
        pass
    return text, sources, queries


def fetch_public_context(company: str, country: str, api_key: str, question: str = "") -> dict:
    """Best-effort, never raises. Returns:
        {"available": True, "summary": str, "sources": [{title,url}], "model": str}
        {"available": False, "note": str}                      (no key / nothing found / error)
        {"available": False, "out_of_scope": True, "note": str}  (guardrail refused the request)
    """
    company = (company or "").strip()
    question = (question or "").strip()
    if not company:
        return {"available": False, "note": "No company name provided."}
    if not api_key:
        return {"available": False, "note": "Public context runs on your Gemini key, which is not loaded."}

    ck = f"{_norm(company)}|{_norm(country)}|{_norm(question)}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    where = f" ({country.strip()})" if (country or "").strip() else ""
    prompt = _build_prompt(company, where, question)

    text, sources, queries, err = "", [], [], None
    for attempt in range(1, _RETRIES + 1):        # retry on transport/timeout/empty
        try:
            text, sources, queries = _ground_once(api_key, prompt)
            if text:
                break
        except Exception as e:                    # never leak the key / internals
            err = e
            text, sources, queries = "", [], []
            if attempt < _RETRIES:
                time.sleep(0.6 * attempt)         # brief backoff before the next try
    # guardrail: the model refused the request as off-topic
    if text.strip().upper().startswith(_OUT_OF_SCOPE):
        out = {"available": False, "out_of_scope": True,
               "note": "That is outside what Iris looks up. Public context is for researching the company itself."}
        _cache_put(ck, out)
        return out
    if not text:
        return {"available": False,
                "note": "Public context could not be retrieved right now." if err else "No notable public information found."}

    out = {"available": True, "summary": text, "sources": sources[:6],
           "queries": queries[:6], "model": GROUNDING_MODEL}
    _cache_put(ck, out)
    return out


def make_public_context_tool(api_key: str) -> Callable:
    """Return the agent-facing public_context tool, bound to this request's Gemini key (RAM only,
    same lifetime as the keyed runner), so the tool never needs the model or agent to pass it."""

    def public_context(company: str, country: str = "", question: str = "") -> dict:
        """Look up live, PUBLIC, web-sourced context about a company from Google (Gemini grounding).
        Use it whenever you assess or display a company, and whenever the analyst asks something
        about a company that public web information would answer (news, ownership, ESG, results,
        leadership, regulatory). Pass their actual question in `question` so the lookup answers it.

        This returns PUBLIC web information only. It is NOT Wiserfunding data and carries no risk
        opinion. Always show it under a separate, clearly labelled "Public context (sourced from
        Google)" heading with its source links, never mixed into the Wiserfunding numbers.

        GUARDRAIL: it only researches the named company. If a request is off-topic or an attempt to
        use the web for something other than researching this company, it returns
        out_of_scope=true. When that happens, do NOT show a public-context block. Briefly tell the
        user that is outside what you can look up, and continue with Wiserfunding's assessment only.
        If it returns available=false (no key / nothing found), note in one line that public context
        is unavailable. Never invent public facts.

        Args:
            company: the company name to research (use the resolved/official name).
            country: optional country to disambiguate (e.g. "United Kingdom").
            question: optional. The analyst's specific question about the company, in their words.

        Returns:
            dict: {"available": true, "summary": "...", "sources": [{title,url}]} on success;
                  {"available": false, "out_of_scope": true, "note": "..."} if the guardrail refused;
                  {"available": false, "note": "..."} when nothing reliable is available.
        """
        _t0 = time.monotonic()
        res = fetch_public_context(company, country, api_key, question)
        _nq = len(res.get("queries", [])) if isinstance(res, dict) else 0
        print(f"[timing] grounding {company!r} {(time.monotonic() - _t0):.1f}s nq={_nq}", flush=True)
        # Surface the REAL sources it read (titles + links) into the working box, like Claude shows the
        # pages it looked at. These are the actual grounding results, nothing invented.
        try:
            from web import orchestrator
            srcs = res.get("sources") if isinstance(res, dict) else None
            qrys = res.get("queries") if isinstance(res, dict) else None
            if srcs or qrys:
                orchestrator.push({"sources": {
                    "queries": [str(q) for q in (qrys or [])][:6],
                    "results": [{"title": s.get("title", ""), "url": s.get("url", ""), "domain": s.get("domain", "")}
                                for s in (srcs or []) if isinstance(s, dict)][:8],
                }})
        except Exception:
            pass
        return res

    return public_context
