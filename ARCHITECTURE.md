# Iris architecture

Iris is delivered to **Wiserfunding's clients** (banks, lenders, asset managers and credit funds), not used internally. A credit officer at a client institution talks to Iris in plain English, by chat or voice, and Iris autonomously produces a sourced credit-risk read.

The flow runs straight down. The client asks at the top. The ADK coordinator delegates a single-company assessment to a parallel team. Lane A reaches out to the Wiserfunding engine and Lane B to Google Search. A synthesiser reconciles the two, and the finished read streams back to the client at the bottom.

```mermaid
%%{init: {'flowchart': {'curve': 'linear'}}}%%
flowchart TD
    Client["<b>CLIENT</b><br/>credit officer at a bank, lender or fund<br/>asks in plain English, by chat or voice"]
    Iris["<b>Iris</b><br/>web app on Google Cloud Run"]
    Coord["<b>Iris Coordinator</b><br/>ADK LlmAgent · routes the conversation"]
    Assess["<b>assess_company</b><br/>ADK AgentTool wrapping a SequentialAgent"]
    Gather["<b>gather</b><br/>ADK ParallelAgent · runs both lanes at once"]
    LaneA["<b>Data Analyst</b> · ADK LlmAgent<br/><b>LANE A · proprietary assessment</b><br/>read-only Wiserfunding tools"]
    LaneB["<b>Web Context</b> · ADK LlmAgent<br/><b>LANE B · cited public context</b><br/>Gemini + Google Search"]
    Synth["<b>Risk Synthesiser</b> · ADK LlmAgent<br/>reconciles Lane A + Lane B"]
    Answer["<b>Sourced risk read</b><br/>streamed back to the client"]

    WF["<b>WISERFUNDING ENGINE</b><br/>proprietary risk API + models<br/>SME Z-Score · PD · LGD · rating · limit<br/>reached over an authenticated, per-tenant MCP boundary<br/><i>every assessment depends on this · credential-gated · not in this repo</i>"]
    GS["<b>Google Search</b><br/>live public context"]

    Client -->|asks about a company| Iris
    Iris --> Coord
    Coord -->|single-company assessment| Assess
    Assess --> Gather
    Gather --> LaneA
    Gather --> LaneB
    LaneA -.->|authenticated MCP call| WF
    LaneA --> Synth
    LaneB --> Synth
    LaneB -.->|grounding| GS
    Synth --> Answer

    classDef client fill:#ffffff,stroke:#26AEB1,stroke-width:2px,color:#0B0E0F;
    classDef adk fill:#ffffff,stroke:#26AEB1,stroke-width:1.5px,color:#0B0E0F;
    classDef laneA fill:#26AEB1,stroke:#17686A,color:#ffffff;
    classDef laneB fill:#E58A2E,stroke:#b96d1f,color:#ffffff;
    classDef synth fill:#17686A,stroke:#0B0E0F,color:#ffffff;
    classDef answer fill:#ffffff,stroke:#17686A,stroke-width:2px,color:#0B0E0F;
    classDef boundary fill:#F4F7F7,stroke:#0B0E0F,stroke-width:1.5px,stroke-dasharray:5 4,color:#0B0E0F;
    classDef engine fill:#0B0E0F,stroke:#E3B23C,stroke-width:3px,color:#ffffff;

    class Client client;
    class Iris,Coord,Assess,Gather adk;
    class LaneA laneA;
    class LaneB laneB;
    class Synth synth;
    class Answer answer;
    class GS boundary;
    class WF engine;
```

**Everything runs on the Wiserfunding engine.** Lane A's every number comes from Wiserfunding's proprietary risk API, reached over an authenticated, per-tenant MCP boundary. No credential, no assessment. The contest judges get a demo login so they can see it work; in production, only Wiserfunding's paying clients hold a key. That is the point: the agent is the interface, the Wiserfunding engine is the product, and nothing the agent does is possible without it.

*Reading the diagram: the coloured boxes are our ADK agents (teal Lane A, orange Lane B, dark teal synthesiser). The gold-edged black box is the Wiserfunding engine, the proprietary, credential-gated core that every assessment depends on (not in this repo). The dashed box on the right is the external Google Search boundary. Dashed arrows are the agent reaching out to an external service; solid arrows are the assessment flowing down to the client.*

**Why two lanes.** Lane A is Wiserfunding's proprietary, authoritative assessment (the SME Z-Score, PD, LGD, bond-rating equivalent, indicative limit). Lane B is live, cited, public web context. They run in parallel and stay strictly separate, so the proprietary numbers and the public facts never blur. The synthesiser produces a read that neither lane could produce alone, which is the core argument for the multi-agent design.

## The model layer (model-agnostic)

The ADK agents are not tied to one model. Per request, the runtime (`model_resolver.py`) builds the LLM client from the client's choice and discards it:

```mermaid
%%{init: {'flowchart': {'curve': 'linear'}}}%%
flowchart LR
    Req["client request"] --> Resolver["model_resolver.py<br/>per-request, key never stored"]
    Resolver -->|recommended| Gemini["<b>Gemini</b><br/>BYOK · best quality, context window,<br/>price, and Gemini Live voice"]
    Resolver -->|privacy / on-prem| Gemma["<b>Local open models</b><br/>Gemma 4 12B, or a 4B lean profile<br/>via an OpenAI-compatible endpoint"]

    classDef a fill:#ffffff,stroke:#26AEB1,color:#0B0E0F;
    classDef g fill:#26AEB1,stroke:#17686A,color:#ffffff;
    classDef l fill:#F4F7F7,stroke:#0B0E0F,color:#0B0E0F;
    class Req,Resolver a;
    class Gemini g;
    class Gemma l;
```

- **Bring-your-own-key Gemini (recommended).** Clients keep control of cost and data, we never meter or store the key (there is no key column anywhere, it is structural). We recommend Gemini because it gives the best report quality, the largest context window to hold a full assessment, and the strongest quality-for-price. **Gemini Live** powers the voice layer, which is unmatched for a hands-free analyst.
- **Local open models (privacy / on-prem).** For clients with data-residency or privacy constraints, Iris runs entirely on a self-hosted open model with no external key. It is validated on **Gemma 4 (12B)**, and we tuned a **lean profile for 4B-class models** with a lighter system prompt and a read-only tool slice so a small on-box model stays reliable.
- **Custom local endpoint.** A client can point Iris at their own OpenAI-compatible endpoint. This path is guarded by a strict SSRF filter (`validate_base_url`): https-only, the host is resolved and every resolved IP checked against private, loopback, link-local, cloud-metadata and IPv4-mapped-IPv6 ranges before any call.

The two-lane assessment, the guardrails, and the streamed output are identical across models. The model is a swappable backend; the agent design is the product.
