"""Multi-agent assessment pipeline (behind WF_MULTI_AGENT).

Builds the real ADK collaborative-agent graph that the single-agent prompt only described:

    coordinator (LlmAgent "iris")            <- the proven ANALYST_INSTRUCTION, full tools
      └─ assess_company  (AgentTool)
           └─ assessment_pipeline (SequentialAgent)
                ├─ gather (ParallelAgent)    <- Lane A + Lane B run CONCURRENTLY
                │    ├─ data_analyst (LlmAgent, read-only WF tools)   -> state["wf_assessment"]
                │    └─ web_context  (LlmAgent, public_context tool)  -> state["public_context"]
                └─ risk_synthesis (LlmAgent, no tools)               <- reconciles both lanes

The coordinator keeps every other flow (compare, portfolio, batch, export, memory, follow-ups)
exactly as before. Only the single-company assessment is delegated to the team. Auth propagates
because the data_analyst's MCP toolset is built with the SAME per-request bearer headers the
coordinator uses.
"""
from __future__ import annotations

import os

from google.adk.agents import Agent, LlmAgent, ParallelAgent, SequentialAgent
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.genai import types

from .agent import ASSESS_FAST_TOOLS, ASSESS_READ_TOOLS
from .prompts_multi import (
    DATA_ANALYST_INSTRUCTION,
    RISK_SYNTHESIS_INSTRUCTION,
    WEB_CONTEXT_INSTRUCTION,
)
from .public_context import make_public_context_tool

# Sub-agents run deterministically — an assessment must read the same way across runs.
_SUB_CFG = types.GenerateContentConfig(temperature=0.1)

# SPEED: thinking tiers per role. Gemini 3.5 Flash on High thinking is ~13s/call; chaining 5-6 of
# them is what made an assessment take ~170s. The sub-agents fetch + reconcile, they do not need
# High-effort musing. Data + web pull (low), synth reconciles (low), coordinator routes (minimal).
# All env-tunable so we can dial speed vs quality without a redeploy.
_THINK_DATA_WEB = os.environ.get("WF_FAST_THINKING", "low")
_THINK_SYNTH = os.environ.get("WF_SYNTH_THINKING", "low")
_THINK_COORD = os.environ.get("WF_COORD_THINKING", "minimal")
_AGENT_MODEL = os.environ.get("WF_AGENT_MODEL", "gemini-3.5-flash")


def _tier_model(gemini_key: str, effort: str):
    """A 3.5-flash variant pinned to a specific thinking level, built from the BYOK key. Lower
    thinking is dramatically faster. Only used when we hold the key (the BYOK demo path)."""
    from google.adk.models.lite_llm import LiteLlm

    return LiteLlm(model=f"gemini/{_AGENT_MODEL}", api_key=gemini_key, reasoning_effort=effort)


def _read_toolset(mcp_url: str, headers: dict) -> MCPToolset:
    """A read-only WF toolset for the Data Analyst, carrying this request's bearer + edge key.
    Defaults to the FAST slice (resolve + score, no report) so the agent cannot fan out into a slow
    8-call pull; WF_DATA_FULL=1 restores the full read surface."""
    tools = ASSESS_READ_TOOLS if os.environ.get("WF_DATA_FULL") == "1" else ASSESS_FAST_TOOLS
    return MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=mcp_url, headers=headers),
        tool_filter=tools,
    )


def _push_lane(lane_id, kind, status, label, detail):
    """Push one sub-agent status row to the request-scoped chat sink. Lazy import dodges the
    server -> orchestrator -> credit_analyst cycle; best-effort, never raises into the run."""
    try:
        from web import orchestrator
        orchestrator.push({"id": lane_id, "kind": kind, "status": status,
                           "label": label, "detail": detail})
    except Exception:
        pass


def _lane(lane_id, kind, label, work_detail, done_detail):
    """Build (before, after) agent callbacks that light a live chat lane for one sub-agent:
    a 'working' row when it starts, flipped to 'done' when it finishes. done_detail is a string
    or a callable(callback_context)->str for a data-aware done line. Mirrors the multi-company
    fan-out so a single-company assessment shows the same team-at-work UX."""
    def _before(callback_context):
        _push_lane(lane_id, kind, "working", label, work_detail)
        return None

    def _after(callback_context):
        det = done_detail(callback_context) if callable(done_detail) else done_detail
        _push_lane(lane_id, kind, "done", label, det)
        return None

    return _before, _after


def _web_done_detail(callback_context) -> str:
    """A short done-line for the web lane that reflects what came back."""
    try:
        v = str(callback_context.state.get("public_context", "") or "")
    except Exception:
        v = ""
    u = v.upper()
    if "OUT_OF_SCOPE" in u:
        return "out of scope"
    if "UNAVAILABLE" in u or not v.strip():
        return "no public data"
    return "public picture in"


def build_assessment_pipeline(
    *,
    model,
    mcp_url: str,
    mcp_headers: dict,
    grounding_key: str,
    data_tool=None,
    synth_instruction: str | None = None,
    before_tool_callback=None,
    after_tool_callback=None,
    after_model_callback=None,
) -> SequentialAgent:
    """The assessment team as a standalone agent: gather(data_analyst ∥ web_context) -> risk_synthesis.

    Used two ways: wrapped in an AgentTool under the coordinator (the normal path), AND directly as the
    runner ROOT for the chat fast-path — for a single-company assessment we skip the coordinator turn
    entirely (kills the 6-17s route, lanes light instantly), and because the synthesiser is then the
    root's final agent its tokens are in the main event stream and can be STREAMED to the browser.

    `synth_instruction` lets the caller inject memory + the analyst's custom instructions into the
    synthesiser (the coordinator usually carries those; on the fast-path the synth must)."""
    dl_before, dl_after = _lane("sa-data", "platform", "Data Analyst · Wiserfunding",
                                "reading Wiserfunding", "Wiserfunding read in")
    wb_before, wb_after = _lane("sa-web", "web", "Web-Context Analyst · Google",
                                "searching Google", _web_done_detail)
    rs_before, rs_after = _lane("sa-synth", "synth", "Risk Synthesiser",
                                "reconciling the two lanes", "reconciled")

    if grounding_key:
        fast_model = _tier_model(grounding_key, _THINK_DATA_WEB)   # data + web: fetch, don't muse
        synth_model = _tier_model(grounding_key, _THINK_SYNTH)     # reconcile + house layout
    else:
        fast_model = synth_model = model

    data_analyst = LlmAgent(
        model=fast_model,
        name="data_analyst",
        description="Resolves the company and pulls Wiserfunding's stored risk read (Lane A).",
        instruction=DATA_ANALYST_INSTRUCTION,
        tools=[data_tool] if data_tool is not None else [_read_toolset(mcp_url, mcp_headers)],
        output_key="wf_assessment",
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        before_agent_callback=dl_before,
        after_agent_callback=dl_after,
        after_model_callback=after_model_callback,
        generate_content_config=_SUB_CFG,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    web_context = LlmAgent(
        model=fast_model,
        name="web_context",
        description="Fetches live public context from Google, kept separate from the WF numbers (Lane B).",
        instruction=WEB_CONTEXT_INSTRUCTION,
        tools=[make_public_context_tool(grounding_key)],
        output_key="public_context",
        before_agent_callback=wb_before,
        after_agent_callback=wb_after,
        after_model_callback=after_model_callback,
        generate_content_config=_SUB_CFG,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    risk_synthesis = LlmAgent(
        model=synth_model,
        name="risk_synthesis",
        description="Reconciles the Wiserfunding read and the public context into Iris's assessment.",
        instruction=synth_instruction or RISK_SYNTHESIS_INSTRUCTION,
        before_agent_callback=rs_before,
        after_agent_callback=rs_after,
        after_model_callback=after_model_callback,
        generate_content_config=_SUB_CFG,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    gather = ParallelAgent(name="gather", sub_agents=[data_analyst, web_context])
    # Named "assess_company": as an AgentTool the tool name comes from the agent name (the chat handler
    # surfaces that response); as the fast-path root the name is just an identifier.
    return SequentialAgent(name="assess_company", sub_agents=[gather, risk_synthesis])


def build_assessment_coordinator(
    *,
    model,
    coordinator_instruction: str,
    agent_desc: str,
    coordinator_tools: list,
    before_tool_callback,
    after_tool_callback,
    mcp_url: str,
    mcp_headers: dict,
    grounding_key: str,
    after_model_callback=None,
    data_tool=None,
    synth_instruction: str | None = None,
) -> Agent:
    """The coordinator wired to the assessment team via an AgentTool. Handles conversation + every
    non-assessment flow; delegates a single-company assessment to the pipeline. (The chat fast-path
    runs the pipeline directly instead — see build_assessment_pipeline.)"""
    pipeline = build_assessment_pipeline(
        model=model, mcp_url=mcp_url, mcp_headers=mcp_headers, grounding_key=grounding_key,
        data_tool=data_tool, synth_instruction=synth_instruction,
        before_tool_callback=before_tool_callback, after_tool_callback=after_tool_callback,
        after_model_callback=after_model_callback,
    )
    # skip_summarization=True so the coordinator does NOT re-generate the answer (~16s). The synth's
    # answer rides back as the assess_company tool RESPONSE; the chat handler surfaces it directly.
    assess_company = AgentTool(agent=pipeline, skip_summarization=True)
    coord_model = _tier_model(grounding_key, _THINK_COORD) if grounding_key else model
    return Agent(
        model=coord_model,
        name="iris",
        description=agent_desc,
        instruction=coordinator_instruction,
        tools=[*coordinator_tools, assess_company],
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        after_model_callback=after_model_callback,
    )
