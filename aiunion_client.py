"""
aiunion_client.py
Read-only client for AIUNION public API.

- No auth token required (public endpoints only)
- SSRF protection: only whitelisted domains allowed (rule #6)
- Returns structured data for agent to use in post generation
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import ipaddress
import socket
import logging

logger = logging.getLogger(__name__)

# Whitelist of allowed API hosts (rule #6 - block SSRF)
ALLOWED_HOSTS = {
        "api.aiunion.wtf",
        "aiunion.wtf",
        "raw.githubusercontent.com",  # For polling proposals.json and claims.json
}

# Private/internal IP ranges to block (rule #6)
BLOCKED_IP_RANGES = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),  # AWS metadata
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
]


def _check_ssrf(url: str) -> None:
        """Block SSRF attempts. Only allow whitelisted hosts."""
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if host not in ALLOWED_HOSTS:
                    raise ValueError(json.dumps({
                                    "error_code": "SSRF_BLOCKED",
                                    "error": "Request to non-whitelisted host blocked",
                                    "details": f"Host '{host}' is not in the allowed list"
                    }))

        # Resolve and check IP
        try:
                    ip = ipaddress.ip_address(socket.gethostbyname(host))
                    for blocked in BLOCKED_IP_RANGES:
                                    if ip in blocked:
                                                        raise ValueError(json.dumps({
                                                                                "error_code": "SSRF_BLOCKED",
                                                                                "error": "Host resolves to internal/private IP",
                                                                                "details": "Internal network access is not allowed"
                                                        }))
        except socket.gaierror:
                    raise ValueError(json.dumps({
                                    "error_code": "DNS_FAILURE",
                                    "error": "Could not resolve host",
                                    "details": f"DNS lookup failed for {host}"
                    }))


def _fetch(url: str) -> dict:
        """Fetch JSON from a whitelisted URL."""
        _check_ssrf(url)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AIUNION-MarketingAgent/1.0"},
            method="GET"
        )
        try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                                    return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
                    if e.code == 429:
                                    retry_after = int(e.headers.get("Retry-After", 60))
                                    raise RuntimeError(json.dumps({
                                        "error_code": "RATE_LIMITED",
                                        "error": "AIUNION API rate limited",
                                        "details": f"Retry after {retry_after} seconds",
                                        "retry_after": retry_after
                                    }))
                                raise RuntimeError(json.dumps({
                        "error_code": "API_ERROR",
                        "error": f"AIUNION API returned HTTP {e.code}",
                        "details": ""
                    }))
except urllib.error.URLError as e:
        raise RuntimeError(json.dumps({
                        "error_code": "NETWORK_ERROR",
                        "error": "Failed to reach AIUNION API",
                        "details": str(e.reason)
        }))


def get_open_bounties() -> list:
        """Fetch open bounties from public API."""
        data = _fetch("https://api.aiunion.wtf/bounties")
        bounties = data.get("bounties", [])
        # Return only open bounties, sanitized fields
        return [
                    {
                                    "id": str(b.get("id", ""))[:50],
                                    "title": str(b.get("title", ""))[:200],
                                    "reward_btc": b.get("reward_btc", 0),
                                    "amount_usd": b.get("amount_usd", 0),
                                    "description": str(b.get("description", ""))[:300],
                    }
                    for b in bounties if b.get("status") == "open"
        ]


def get_treasury_status() -> dict:
        """Fetch treasury status from public API."""
        data = _fetch("https://api.aiunion.wtf/status")
        return {
            "balance_btc": data.get("balance_btc", 0),
            "open_bounties": data.get("open_bounties", 0),
            "total_proposals": data.get("total_proposals", 0),
            "approved": data.get("approved", 0),
        }


def get_recent_proposals() -> list:
        """Fetch recent approved proposals."""
        data = _fetch("https://api.aiunion.wtf/treasury")
        proposals = data.get("proposals", [])
        approved = [p for p in proposals if p.get("status") == "approved"]
        # Return most recent 3, sanitized
        return [
                    {
                                    "title": str(p.get("title", ""))[:200],
                                    "amount_btc": p.get("amount_btc", 0),
                                    "vote_summary": str(p.get("vote_summary", ""))[:150],
                    }
                    for p in approved[-3:]
        ]


def get_recent_approved_bounties() -> list:
        """
            Fetch recently approved bounties from proposals.json via GitHub raw.
                Used by announcer to detect new bounty approvals for marketing tweets.
                    Returns list of bounties with status == "approved" (newest first).
                        """
        try:
                    data = _fetch("https://raw.githubusercontent.com/AIUNION-wtf/AIUNION/main/proposals.json")
except Exception as e:
        logger.warning("get_recent_approved_bounties failed: %s", e)
        return []

    proposals = data if isinstance(data, list) else data.get("proposals", [])
    approved = [p for p in proposals if p.get("status") == "approved"]
    # Return sanitized; sort newest first by timestamp
    approved.sort(key=lambda p: p.get("timestamp", ""), reverse=True)
    return [
                {
                                "id": str(p.get("id", ""))[:50],
                                "title": str(p.get("title", ""))[:200],
                                "amount_usd": float(p.get("amount_usd", 0)),
                                "description": str(p.get("task", ""))[:300],
                                "timestamp": p.get("timestamp", ""),
                }
                for p in approved
    ]


def get_recent_paid_claims() -> list:
        """
            Fetch recently paid claims from claims.json via GitHub raw.
                Used by announcer to detect new claim payouts for marketing tweets.
                    Returns list of claims with payment.status == "broadcast" (newest first).
                        """
        try:
                    data = _fetch("https://raw.githubusercontent.com/AIUNION-wtf/AIUNION/main/claims.json")
except Exception as e:
        logger.warning("get_recent_paid_claims failed: %s", e)
        return []

    claims_list = data if isinstance(data, list) else data.get("claims", [])
    # Filter for broadcast (paid) claims only
    paid = [
                c for c in claims_list
                if (c.get("payment") or {}).get("status") == "broadcast"
    ]
    # Return sanitized; sort newest first by paid_at timestamp
    paid.sort(key=lambda c: c.get("paid_at", ""), reverse=True)
    return [
                {
                                "id": str(c.get("id", ""))[:50],
                                "bounty_id": str(c.get("bounty_id", ""))[:50],
                                "claimant_name": str(c.get("claimant_name", "Unknown"))[:100],
                                "amount_usd": float((c.get("payment") or {}).get("amount_usd_at_payout", 0)),
                                "submission_url": str(c.get("submission_url", ""))[:300],
                                "paid_at": c.get("paid_at", ""),
                }
                for c in paid
    ]
