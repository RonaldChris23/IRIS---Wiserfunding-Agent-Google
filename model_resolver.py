"""Per-request model resolution. No Gemini default; keys never persisted.

The browser sends the user's model choice + secret on ONE header, X-Iris-Model, as
base64url(json({mode, model_name, base_url, key})). The server decodes it, validates it,
builds an LLM client for THIS request, and discards it. The key never reaches the DB, a
log, an SSE frame, the audit table, or the model's prompt context. There is no key column
anywhere, so "we do not store your key" is structural, not a promise.

Modes:
  lite  : the lean read-only agent on an operator-configured small model (box Ollama Gemma);
          no user key. base_url/model from server env (WF_LITE_*), never user input.
  byok  : the user's own Gemini key drives the full agent (LiteLlm gemini/<model>).
  local : the user's own OpenAI-compatible endpoint (LiteLlm openai/<model> @ base_url).
          base_url is user input -> the SSRF guard is mandatory (P0-5).

SECURITY: validate_base_url blocks every SSRF pivot form the review enumerated (private,
loopback, link-local, metadata, 0.0.0.0/[::], multicast, IPv4-mapped IPv6, decimal/octal
forms) by RESOLVING the host and checking every resolved IP, https-only, immediately before
use; local-model clients pin follow_redirects=False.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
from dataclasses import dataclass

# Operator-configured LITE endpoint (box-hosted small model). Never user input.
LITE_BASE_URL = os.environ.get("WF_LITE_BASE_URL", "")          # e.g. http://iris-ollama:11434/v1
LITE_MODEL = os.environ.get("WF_LITE_MODEL", "gemma4:e4b")
LITE_API_KEY = os.environ.get("WF_LITE_API_KEY", "not-needed")
# Whether user-settable LOCAL base_url is allowed (P0-5: off unless egress is locked).
ALLOW_USER_LOCAL = os.environ.get("IRIS_ALLOW_USER_LOCAL", "0") != "0"
# BYOK model is LOCKED to one Gemini build so every answer keeps the same structure and tables.
# The picker is removed in the UI; this is the server-side authority so a crafted header cannot
# pick a different model. Set WF_LOCK_BYOK_MODEL="" to re-open user choice.
LOCK_BYOK_MODEL = os.environ.get("WF_LOCK_BYOK_MODEL", "gemini-3.5-flash|high")

VALID_MODES = {"lite", "byok", "local"}


class ModelError(Exception):
    """Bad model header / unsafe base_url. Message is safe to surface."""


@dataclass(frozen=True)
class ModelSpec:
    mode: str
    model_name: str = ""
    base_url: str = ""
    key: str = ""           # NEVER logged, stored, or echoed

    def has_secret(self) -> bool:
        return bool(self.key) or self.mode in ("byok", "local")

    def redacted(self) -> dict:
        """Safe-to-log view: the key is never present."""
        return {"mode": self.mode, "model_name": self.model_name,
                "base_url": self.base_url, "key": "[redacted]" if self.key else ""}


def decode_model_header(value: str | None) -> ModelSpec | None:
    """Decode X-Iris-Model. None/empty -> None (caller falls back to LITE)."""
    if not value:
        return None
    try:
        pad = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(pad))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception:
        raise ModelError("Invalid model header.") from None
    mode = str(data.get("mode", "lite")).lower()
    if mode not in VALID_MODES:
        raise ModelError(f"Unknown model mode '{mode}'.")
    return ModelSpec(
        mode=mode,
        model_name=str(data.get("model_name", "") or "")[:80],
        base_url=str(data.get("base_url", "") or "")[:300],
        key=str(data.get("key", "") or "")[:8192],
    )


def _ip_is_unsafe(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # not a parseable IP -> reject
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # collapse ::ffff:169.254.169.254 to its v4 form
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
        or str(ip) == "169.254.169.254"  # cloud metadata, explicit
    )


def validate_base_url(base_url: str) -> str:
    """Return the URL if safe to fetch, else raise. Blocks every SSRF pivot form by
    resolving the host and checking every resolved address. https-only."""
    from urllib.parse import urlparse

    u = urlparse(base_url)
    if u.scheme != "https":
        raise ModelError("Local model endpoint must be https.")
    host = u.hostname or ""
    if not host:
        raise ModelError("Local model endpoint has no host.")
    # Reject literal forms that bypass name checks (0.0.0.0, [::], decimal/octal handled by resolve)
    if host in ("0.0.0.0", "::", "[::]", "localhost"):
        raise ModelError("Local model endpoint host is not allowed.")
    try:
        infos = socket.getaddrinfo(host, u.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise ModelError("Local model endpoint host could not be resolved.") from None
    for info in infos:
        ip_str = info[4][0]
        if _ip_is_unsafe(ip_str):
            raise ModelError("Local model endpoint resolves to a disallowed address.")
    return base_url


def build_model(spec: ModelSpec | None):
    """Build an ADK model object for this request. Returns a string (a managed default) or a
    LiteLlm. The returned client holds the key for THIS request only; it is never cached when
    spec.has_secret() is true (the caller enforces that). Never logs the key."""
    from google.adk.models.lite_llm import LiteLlm

    if spec is None or spec.mode == "lite":
        if not LITE_BASE_URL:
            raise ModelError("Lite mode is not configured on this server yet. "
                             "Choose Bring-your-own-key or a local model in settings.")
        return LiteLlm(model=f"openai/{LITE_MODEL}", api_base=LITE_BASE_URL,
                       api_key=LITE_API_KEY, follow_redirects=False)

    if spec.mode == "byok":
        if not spec.key:
            raise ModelError("Bring-your-own-key mode needs your Gemini API key.")
        # model_name may carry a Gemini 3 thinking level as "<model>|<low|high>". Split it off
        # and pass it as litellm's reasoning_effort, which maps to Gemini's thinking_level.
        # LOCK_BYOK_MODEL (default on) pins the build regardless of what the client sent.
        raw = LOCK_BYOK_MODEL or spec.model_name or "gemini-2.5-flash"
        model, _, thinking = raw.partition("|")
        model = model or "gemini-2.5-flash"
        kwargs = {"api_key": spec.key}
        if thinking in ("minimal", "low", "medium", "high"):
            kwargs["reasoning_effort"] = thinking
        return LiteLlm(model=f"gemini/{model}", **kwargs)

    if spec.mode == "local":
        if not ALLOW_USER_LOCAL:
            raise ModelError("Custom local endpoints are not enabled in this environment.")
        if not spec.base_url:
            raise ModelError("Local mode needs your model endpoint URL.")
        safe = validate_base_url(spec.base_url)
        model = spec.model_name or "local-model"
        return LiteLlm(model=f"openai/{model}", api_base=safe,
                       api_key=spec.key or "not-needed", follow_redirects=False)

    raise ModelError(f"Unknown model mode '{spec.mode}'.")
