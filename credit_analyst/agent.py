"""Iris - an autonomous SME credit-risk analyst agent.

Google ADK + Gemini. Every Wiserfunding call goes through the wiserfunding-mcp
server (the secure tool boundary): the agent never touches the raw WF API, only the
curated, audited, spend-capped MCP tools. The WF scoring engine stays a black box.

`adk web` / `adk run` discover `root_agent` from this module.
"""
from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    # agent/.env then agent/.env.local (local wins). Neither is committed.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(here, ".env"))
    load_dotenv(os.path.join(here, ".env.local"), override=True)
except Exception:
    pass

from google.adk.agents import Agent
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset

from .prompt import ANALYST_INSTRUCTION

MCP_URL = os.environ.get("WF_MCP_URL", "http://127.0.0.1:8799/mcp")
MCP_EDGE_KEY = os.environ.get("WF_MCP_EDGE_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# The curated slice of the MCP's 47 tools this analyst is allowed to see. Keeping the
# surface tight makes tool selection reliable and reinforces the safety story: the
# agent only ever sees read + assess + portfolio tools, never the full API breadth.
DEMO_TOOLS = [
    # account + company lookup (free)
    "wf_whoami",
    "wf_get_credit_balance",             # live credit balance (free; real total/spent/remaining)
    "wf_search_companies",
    "wf_get_company_score",
    "wf_get_company",
    "wf_get_company_portfolios",
    # reports (reads free; generate* billable)
    "wf_list_reports",
    "wf_get_report",
    "wf_get_report_section",
    # wf_export_report removed: it returns base64 PDF/Excel that floods the LLM context
    # (>1M tokens). Exports are handled out-of-band via a download link, not through Iris.
    "wf_generate_report",                # billable - confirm first
    "wf_generate_report_minimal",        # billable - confirm first
    "wf_generate_report_from_financials",  # billable - score from supplied figures
    # batch (one-off bulk scoring; distinct from portfolios)
    "wf_create_batch",                   # billable - 1 credit/company, confirm first (ADS)
    "wf_create_batch_from_financials",   # billable - 1 credit/company, confirm first (MDI)
    "wf_get_batch_status",               # free - poll
    "wf_get_batch_results",              # free - per-company scores
    "wf_list_batches",                   # free - list/search batch jobs
    # wf_export_batch deliberately omitted: it returns a large CSV that floods the LLM context
    # (same rule as wf_export_report). Batch export is served out-of-band via /api/export?batch_id.
    # portfolio monitoring + alerts
    "wf_list_portfolios",
    "wf_get_portfolio",
    "wf_create_portfolio",
    "wf_add_company_to_portfolio",
    "wf_list_portfolio_companies",
    "wf_list_portfolio_runs",
    "wf_refresh_portfolio_monitoring",   # billable - confirm first
    "wf_generate_portfolio_report",      # billable - confirm first
    "wf_get_portfolio_report",
    "wf_list_alerts",
    "wf_get_alert",
    "wf_get_alert_report",
]

# Read-only WF slice for the multi-agent Data Analyst sub-agent (WF_MULTI_AGENT pipeline).
# It RESOLVES + READS only: no billable generate*, no batch, no portfolio writes. Billable
# generation stays with the coordinator and its confirmation flow.
ASSESS_READ_TOOLS = [
    "wf_whoami",
    "wf_get_credit_balance",
    "wf_search_companies",
    "wf_get_company_score",
    "wf_get_company",
    "wf_get_company_portfolios",
    "wf_list_reports",
    "wf_get_report",
    "wf_get_report_section",
]

# FAST slice for the Data Analyst: resolve + score ONLY. No report/section/list tools, so the agent
# physically cannot fan out into an 8-call, 33k-token pull (which then made the synthesiser eat 45k
# tokens / ~22s). The headline assessment (Z-Score, rating, PD, LGD, expected loss, indicative limit)
# comes from the score. Set WF_DATA_FULL=1 to restore the full ASSESS_READ_TOOLS surface.
ASSESS_FAST_TOOLS = [
    "wf_search_companies",
    "wf_get_company_score",
    "wf_get_company",
    "wf_get_credit_balance",
]

# Focused, ordered read-only subset for small local models (lean profile).
# No billable, batch, integration, or subscriber tools — lean mode is read-only by design.
LEAN_TOOLS = [
    "wf_whoami",
    "wf_search_companies",
    "wf_get_company_score",
    "wf_get_company",
    "wf_list_reports",
    "wf_list_report_history",
    "wf_get_report",
    "wf_get_report_section",
    "wf_list_portfolios",
    "wf_get_portfolio",
    "wf_list_portfolio_companies",
    "wf_list_alerts",
]


wf_tools = MCPToolset(
    connection_params=StreamableHTTPConnectionParams(
        url=MCP_URL,
        headers={"X-WF-MCP-Key": MCP_EDGE_KEY} if MCP_EDGE_KEY else {},
    ),
    tool_filter=DEMO_TOOLS,
)

root_agent = Agent(
    model=MODEL,
    name="iris",
    description=(
        "Autonomous SME credit-risk analyst. Resolves companies, pulls Wiserfunding "
        "risk assessments over MCP, produces risk intelligence reports, and monitors "
        "lending portfolios for deterioration."
    ),
    instruction=ANALYST_INSTRUCTION,
    tools=[wf_tools],
)
