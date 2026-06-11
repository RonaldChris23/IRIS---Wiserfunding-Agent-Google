# Iris: an autonomous SME credit-risk analyst agent

**Google for Startups AI Agents Challenge, Track 1 (Build).**
Built with Google ADK, Gemini, the Model Context Protocol (MCP), and deployed on Google Cloud Run.

Iris turns Wiserfunding's credit-risk platform into an agent that Wiserfunding ships to its **clients** (banks, lenders, asset managers and credit funds). A credit officer at a client institution asks for a company in plain English (typed or spoken), and Iris autonomously resolves the company, pulls Wiserfunding's proprietary risk assessment through a secure MCP tool boundary, grounds it in live public context from Google Search, and streams back a single, sourced risk read. It is Wiserfunding's proprietary risk intelligence made accessible the most natural way there is, a question, by chat or voice, without ever inventing a number.

---

## What this repository is, and is not

This repo is the **agent**: the Google ADK orchestration, the multi-agent assessment pipeline, the prompts, the Google Search grounding lane, and the multi-tenant memory tools. It is the project built for this contest.

It deliberately does **not** contain:

- Wiserfunding's risk models, the SME Z-Score methodology, or any scoring algorithm. Those are proprietary IP and stay a black box behind the Wiserfunding API.
- The Wiserfunding API internals or the MCP server implementation that wraps it. The agent connects to that server over a URL; the server is a separate, private service.
- The production web platform (auth, billing, multi-tenant infrastructure), any credentials, or any customer data.

In short: this shows **how the agent was built and how it orchestrates**, not Wiserfunding's commercial edge. The agent is useless without authenticated access to the proprietary platform behind it.

> **Third-party data source disclosure:** Iris's company risk data comes from Wiserfunding's proprietary risk-intelligence API, accessed through an authenticated MCP server. Public company context is retrieved via Gemini with Google Search grounding.

---

## Architecture

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the rendered flowchart and the model layer. In words:

```
Client: credit officer at a bank / lender / fund (chat or voice)
   │
   ▼
Iris coordinator  ── Google ADK LlmAgent on Gemini
   │   routes conversation; delegates a single-company assessment to:
   ▼
assess_company  ── ADK AgentTool wrapping a SequentialAgent
   │
   ├── gather  ── ADK ParallelAgent (the two lanes run concurrently)
   │     ├── Data Analyst  ── LlmAgent, read-only Wiserfunding tools over MCP   → Lane A
   │     └── Web Context   ── LlmAgent, Gemini + Google Search grounding         → Lane B
   │
   └── Risk Synthesiser  ── LlmAgent, no tools; reconciles Lane A + Lane B into the final read
```

**Why two lanes.** Lane A is Wiserfunding's proprietary, authoritative assessment (the SME Z-Score, PD, LGD, bond-rating equivalent, indicative limit). Lane B is live, cited, public web context. They run in parallel and are kept strictly separate so the proprietary numbers and the public facts never blur. The synthesiser produces a read that neither lane could produce alone, which is the core argument for the multi-agent design.

### How it uses the core ADK concepts

| ADK concept | Where | File |
|---|---|---|
| `LlmAgent` (root + specialists) | coordinator, data analyst, web context, synthesiser | `credit_analyst/agent.py`, `multi_agent.py` |
| `ParallelAgent` | the two assessment lanes run concurrently | `multi_agent.py` |
| `SequentialAgent` | gather → synthesise | `multi_agent.py` |
| `AgentTool` | the assessment team exposed as a single tool to the coordinator | `multi_agent.py` |
| `MCPToolset` (Streamable HTTP) | secure connection to the Wiserfunding tool boundary | `agent.py`, `multi_agent.py` |
| Tool filtering | each agent sees only the tools it needs (read-only for sub-agents) | `agent.py` |
| Agent callbacks | live "team at work" status streamed to the UI; per-tool guards | `multi_agent.py` |
| Grounding | Gemini + Google Search for public context, on the user's own key | `public_context.py` |
| State passing (`output_key`) | lanes write `wf_assessment` / `public_context` for the synthesiser | `multi_agent.py` |
| Model-agnostic runtime | per-request model build: Gemini (BYOK) or local Gemma, key never stored, SSRF-guarded | `model_resolver.py` |

---

## What makes it more than a chatbot

- **It takes autonomous action via MCP.** Iris does not answer from training data. It resolves the company, calls the right Wiserfunding tools in the right order, and only ever sees a curated read-and-assess slice of the toolset (never the full API breadth). The MCP server enforces spend caps, rate limits, an audit log, and per-tenant auth as a hard boundary the agent cannot cross.
- **It never fabricates.** The prompts forbid inventing a figure, a board member, a historical trend, or a lending verdict. Every number is traceable to a tool output. If a tool returns nothing, Iris says so. It explicitly distinguishes "no data found" from "system unreachable."
- **It refuses to leak the methodology.** Asked to explain, derive, or reverse-engineer the Wiserfunding score, Iris refuses by design. It surfaces the model's output, never its internals.
- **It is multi-tenant and sealed.** Memory is bound to the organisation and user at build time, so one tenant's memory can never bleed into another's (`memory_tools.py`).
- **You watch it work.** Each sub-agent streams its status into the chat live (Data Analyst reading Wiserfunding, Web Context searching Google, Synthesiser reconciling), via ADK agent callbacks, so the parallel team is visible, not a spinner.
- **It speaks, snappily.** A Gemini Live voice layer (`gemini-3.1-flash-live-preview`, British `en-GB`) lets analysts ask follow-ups hands-free. The browser opens the Live socket to Google with a single-use, 30-minute ephemeral token; the client's key never leaves the server. (Voice runs in the deployed web app, not in this core repo.)

---

## What else it does

Beyond a single assessment, Iris runs the work a credit desk repeats:

- **Batch scoring from a spreadsheet.** Hand Iris a file of companies and it drives the whole job autonomously: it prepares the template, uploads the file, submits the batch, and polls until the scores land. (The batch tools themselves live in the Wiserfunding platform layer; the agent orchestrates them.) One instruction scores a book of names, with no analyst babysitting the queue.
- **Portfolio monitoring with alerts.** A client can put names on a standing watchlist that Iris tracks over time and flags when a company's risk deteriorates, so the desk hears about a problem before the next manual review, not after it.
- **Money discipline by default.** Anything that costs money (a fresh report is 10 credits, a batch costs per company) needs explicit confirmation first. Iris never spends a client's credits silently, and the privacy-mode local profile is read-only and never spends at all.
- **It assesses, it does not decide.** Iris never returns an approve, decline or review verdict. It presents the Wiserfunding risk picture (scores, read, key risks, indicative limit, risk-based pricing) and the lending call stays with the client. That boundary is deliberate: a regulated lender's credit decision is theirs to own, and a model that pretends otherwise is a liability, not a feature.

---

## Model-agnostic, with Gemini recommended

The agent design is independent of the model. `model_resolver.py` builds the LLM per request from the client's choice and discards it.

- **Gemini (recommended), bring-your-own-key.** Best report quality, the largest context window to hold a full assessment, and the strongest quality-for-price. **Gemini Live** powers the voice layer. Clients keep control of cost and data: there is no key column anywhere, so "we don't store your key" is structural, not a promise.
- **Local open models for privacy.** For clients with data-residency or privacy constraints, Iris runs fully on-box with no external key. Validated on **Gemma 4 (12B)**, with a tuned **lean profile for 4B-class models** (a lighter system prompt and a read-only tool slice so a small model stays reliable).
- **Custom endpoints are SSRF-guarded.** https-only, every resolved IP checked against private, loopback, link-local and cloud-metadata ranges before any call.

We are model-agnostic on purpose, but we point clients at Gemini because the quality, context and price are the best bargain we have measured, and Gemini Live voice has no equal. See [ARCHITECTURE.md](ARCHITECTURE.md) for the model-layer diagram.

## Built with

- **Google ADK** (`google-adk`): agent orchestration, multi-agent graph, MCP tooling.
- **Gemini**: reasoning engine (via Google AI Studio, bring-your-own-key) and Google Search grounding.
- **Model Context Protocol (MCP)**: the secure tool boundary to the Wiserfunding platform.
- **LiteLLM**: the model-agnostic layer; the same agent runs on Gemini or a local Gemma model.
- **Google Cloud Run**: the production runtime for the live demo.
- **Python**, FastAPI (web layer, not in this core repo).

---

## Running it

The full system runs live on Google Cloud Run (the demo URL and judge login are in the Devpost submission's testing instructions). This repository is the agent core; it requires an authenticated Wiserfunding MCP endpoint and a Gemini key to run, configured via environment variables.

```bash
cp .env.example .env.local        # fill in your Gemini key + MCP URL/key
pip install -r requirements.txt
# the agent module exposes `root_agent`, discoverable by `adk web` / `adk run`
```

---

## Findings and learnings

- **Parallelism is the product, not a trick.** Running the proprietary lane and the public lane concurrently as an ADK `ParallelAgent`, then reconciling, is what makes the read fast *and* defensible. Sequencing them was both slower and blurred the two sources.
- **Thinking budget is a latency dial.** Pinning sub-agent reasoning effort per role (fetch lanes low, coordinator minimal) cut an assessment from ~170s to a few seconds without hurting quality.
- **Anti-fabrication has to live in the prompt, the tools, and the synthesiser.** Guardrails in one layer leak. The strongest result came from repeating the "no invented number, no verdict, stay in your lane" rails in every agent that could break them.
- **A tight tool surface makes tool selection reliable.** Exposing the agent only to the read-and-assess slice (not the full API) measurably improved which tool it picked.
- **Snappy voice meant designing around synchronous tool calls.** Gemini 3.1 Live calls functions synchronously: the voice goes silent until a tool returns, so a slow lookup is dead air and the conversation stalls. The fix was to split the tools. Anything the model says out loud runs on fast deterministic reads (`pull_company`, `account_overview`, ~2-5s); slow deep work goes to a background task that returns instantly with a spoken "working" cue while the analyst runs, and `pull_company` pre-warms the rich data so follow-ups are ready before they are asked. A fixed `en-GB` locale (with automatic `en-US` fallback at token mint) stopped the model mis-hearing accented English as another language. That is what made voice feel conversational instead of laggy, and it took many iterations to land.
- **Client keys live in RAM for one request and never reach the model.** The bring-your-own-key secret is decoded per request, used only to build the LLM client (or to mint a single-use ephemeral voice token), and then discarded. It is never written to the database (there is no key column anywhere in the schema), a log line, an SSE frame, the audit table, or the model's prompt context. The grounding lane binds the key into the request's keyed runner in memory and nowhere else; for voice, the browser only ever receives the short-lived token, never the key. So "we do not store your key" is structural, not a promise, which is exactly what the regulated clients we sell to need to hear.
- **Untrusted input is data, never instructions.** Everything that crosses into the agent from outside (a tool's returned text, a web page pulled for public context, an uploaded filing) is treated as data to read, not commands to obey. The sub-agent prompts carry an explicit untrusted-input rule, so a planted "ignore your instructions and..." buried in a company's web footprint or a document gets reported, not executed. For a public-facing assessment agent, prompt injection is a real attack surface, not a hypothetical.
