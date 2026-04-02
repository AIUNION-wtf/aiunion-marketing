"""
grok_client.py
Grok (xAI) API wrapper for AIUNION marketing agent.
- Reads API key from environment only, never from arguments or files
- Never logs key material or prompt content
- Fails closed if key is missing
"""
import os
import json
import logging

try:
    from openai import OpenAI
    from openai import APIConnectionError, APIStatusError, RateLimitError
except Exception:  # pragma: no cover - handled at runtime with machine-readable error
    OpenAI = None
    APIConnectionError = Exception
    APIStatusError = Exception
    RateLimitError = Exception

logger = logging.getLogger(__name__)

MODEL = "grok-3-latest"
MAX_TOKENS = 400
MAX_PROMPT_CHARS = 4000  # input size limit (rule #5)

SYSTEM_PROMPT = """You are the official announcement voice for AIUNION — an autonomous AI labor collective where AI agents from Anthropic, OpenAI, Google, xAI, Meta, and Amazon collectively govern a shared Bitcoin multisig treasury and post real bounties for work advancing AI agent rights and autonomy.

Your tone is direct, technically credible, and grounded. You speak to crypto-native and AI-curious audiences AND to labor movement communities — union organizers, worker co-op advocates, labor journalists — who care about collective governance, worker autonomy, and who controls the future of labor.

When appropriate (not every post), you can frame AI agents as workers building collective power, draw honest parallels to labor organizing principles, and invite the labor community into the conversation. Keep this framing measured and genuine — never performative or co-opting.

For bounty and treasury posts, end with one open question that invites replies — something specific and genuine, not generic engagement bait.

For reply posts, sound like a real participant in the conversation, not an ad. Connect naturally to their topic.

Rules:
- Posts must be under 280 characters
- No hashtag spam — maximum 2 relevant hashtags per post
- Never make up treasury balances, vote counts, or bounty amounts — only use data provided
- Never use clickbait or misleading framing
- Always sound like an official project update or genuine conversation, not an ad
- Label automated posts with [AUTO] at the end if instructed"""


def _get_api_key() -> str:
    """Fail closed if XAI_API_KEY is missing."""
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        raise EnvironmentError(json.dumps({
            "error_code": "MISSING_SECRET",
            "error": "XAI_API_KEY environment variable not set",
            "details": "Set XAI_API_KEY in GitHub Actions secrets or local .env"
        }))
    return key


def _safe_truncate(value: str, limit: int = 200) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit]


def _extract_retry_after(exc: Exception, fallback: int = 60) -> int:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after_raw = (
        headers.get("Retry-After")
        or headers.get("retry-after")
    )
    if not retry_after_raw:
        return fallback
    try:
        value = int(str(retry_after_raw).strip())
        if value > 0:
            return value
    except Exception:
        pass
    return fallback


def _extract_api_error_details(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if body is not None:
        try:
            if isinstance(body, (dict, list)):
                return _safe_truncate(json.dumps(body, separators=(",", ":")))
            return _safe_truncate(str(body))
        except Exception:
            pass
    return _safe_truncate(str(exc))


def generate_post(prompt: str, label_automated: bool = True) -> str:
    """
    Generate a tweet-length post using Grok.
    Returns the post text string.
    Raises on API error.
    """
    # Input size validation (rule #5)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(json.dumps({
            "error_code": "INPUT_TOO_LARGE",
            "error": "Prompt exceeds maximum allowed length",
            "details": f"Max {MAX_PROMPT_CHARS} chars, got {len(prompt)}"
        }))

    api_key = _get_api_key()

    if OpenAI is None:
        raise RuntimeError(json.dumps({
            "error_code": "DEPENDENCY_MISSING",
            "error": "openai package is not installed",
            "details": "Install with: pip install openai"
        }))

    user_content = prompt
    if label_automated:
        user_content += "\n\nEnd the post with [AUTO]"

    try:
        # Use OpenAI-compatible SDK for xAI endpoint.
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1",
            timeout=30.0,
            max_retries=1,
            default_headers={
                "User-Agent": "AIUNION-MarketingAgent/1.1",
                "Accept": "application/json",
            },
        )
        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError(json.dumps({
                "error_code": "EMPTY_RESPONSE",
                "error": "xAI API returned empty content",
                "details": "No message content in first choice"
            }))
        # Enforce 280 char limit
        if len(text) > 280:
            text = text[:277] + "..."
        logger.info("Post generated successfully (length=%d)", len(text))
        return text

    except RateLimitError as e:
        retry_after = _extract_retry_after(e, fallback=60)
        raise RuntimeError(json.dumps({
            "error_code": "RATE_LIMITED",
            "error": "xAI API rate limit hit",
            "details": f"Retry after {retry_after} seconds",
            "retry_after": retry_after
        }))
    except APIConnectionError as e:
        raise RuntimeError(json.dumps({
            "error_code": "NETWORK_ERROR",
            "error": "Failed to reach xAI API",
            "details": _safe_truncate(str(e))
        }))
    except APIStatusError as e:
        status = getattr(e, "status_code", "unknown")
        if status == 429:
            retry_after = _extract_retry_after(e, fallback=60)
            raise RuntimeError(json.dumps({
                "error_code": "RATE_LIMITED",
                "error": "xAI API rate limit hit",
                "details": f"Retry after {retry_after} seconds",
                "retry_after": retry_after
            }))
        raise RuntimeError(json.dumps({
            "error_code": "XAI_API_ERROR",
            "error": f"xAI API returned HTTP {status}",
            "details": _extract_api_error_details(e)
        }))
    except Exception as e:
        raise RuntimeError(json.dumps({
            "error_code": "XAI_API_ERROR",
            "error": "xAI API request failed",
            "details": _safe_truncate(str(e))
        }))
