"""The `memory` and `recall` agent tools: autonomous curation, sealed to the caller.

Mirrors make_batch_tools: the server builds these per-org closures and passes the plain
functions into Agent(tools=[...]) (ADK auto-wraps them). org_id is bound at build time
(the runner is per-org, so it cannot be wrong). user_id + session_id come from the ADK
session state the SERVER seeded (iris_user_id / iris_session_id), never from the model.
So the model cannot write into another org's or another user's memory.

This module sits beside the agent and must not import web/ (same rule as batch_runtime).
It calls a small writer/reader passed in, so the web layer owns the DB.
"""
from __future__ import annotations

from typing import Callable


def make_memory_tools(org_id: str, writer: Callable, recaller: Callable):
    """writer(org_id, user_id, action, target, content, old_content, session_id) -> dict
       recaller(org_id, user_id, query) -> list[dict]
    Both are provided by the web layer (web.memory) so this module stays web-free."""

    def _identity(tool_context):
        # S5: no 'default' fallback. If the server-seeded user id is missing, return None so the
        # caller refuses the write/read rather than pooling it into a shared 'default' bucket that
        # would bleed one user's memory into another's.
        state = getattr(tool_context, "state", None) or {}
        return state.get("iris_user_id"), state.get("iris_session_id")

    def memory(action: str, target: str, content: str, old_content: str = "", tool_context=None) -> dict:
        """Persist or update a durable fact about THIS organisation or user. Use it PROACTIVELY:
        the MOMENT the analyst states a lasting fact or preference about themselves (their role, how
        they want answers) or their organisation (a lending rule, risk appetite, a sector focus),
        save it. Skip one-off questions, raw numbers already in a report, and personal data beyond a
        first name.

        action: 'add' to remember a new fact, 'replace' to update one (pass old_content with
                the exact prior text), 'remove' to forget one (pass the text to forget).
        target: 'preference' (how THIS user likes reports/answers, e.g. "always show LGD first"),
                'risk_policy' (the ORG's own lending rule or appetite, e.g. "we don't lend below BB"),
                'assessed_company' (log a company you assessed, into the Companies-assessed
                    register; content MUST be "COMPANY NAME | <bond rating> / PD <x>%"),
                'watched_company' (ONLY when the user explicitly asks to track a company),
                'lesson' (a correction or something to do differently next time),
                'note' (an org-level fact worth keeping).
        A company's scores, rating, PD or LGD go ONLY to 'assessed_company', never to
        'risk_policy', 'note' or 'decision' (those are for the org, not for company data).
        content: one short, self-contained sentence.
        Returns {'ok': true, 'stored': '<content>'} or {'ok': false, 'reason': ...}.
        """
        user_id, session_id = _identity(tool_context)
        if not user_id:
            return {"ok": False, "reason": "no identity"}  # S5: refuse rather than pool into 'default'
        try:
            return writer(org_id, user_id, action, target, content, old_content or None, session_id)
        except Exception as exc:  # never break the turn over a memory write
            return {"ok": False, "reason": f"{type(exc).__name__}"}

    def recall(query: str, tool_context=None) -> dict:
        """Search this org and user's OWN past conversations for something discussed before,
        e.g. 'what did we conclude on Bank of Ireland last week?'. Returns prior snippets.
        Only ever searches the caller's own history.
        """
        user_id, _ = _identity(tool_context)
        if not user_id:
            return {"results": []}  # S5: no identity -> search nothing, never another user's history
        try:
            hits = recaller(org_id, user_id, query)
        except Exception:
            hits = []
        return {"results": [{"role": h.get("role"), "text": (h.get("content") or "")[:300]} for h in hits]}

    return [memory, recall]
