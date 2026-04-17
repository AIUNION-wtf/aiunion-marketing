"""
twitter_client.py
X (Twitter) API v2 posting client for AIUNION marketing agent.
- OAuth 1.0a (required for posting on free/pay-per-use tier)
- Write-only: posts to public timeline only, no DM, no read
- Fails closed if any required secret is missing
- Returns machine-readable errors
- Includes Retry-After on 429 responses
- find_reply_target() searches recent tweets by topic (no following endpoint needed)
"""
import os
import json
import time
import hmac
import hashlib
import base64
import random
import urllib.request
import urllib.error
import urllib.parse
import logging

logger = logging.getLogger(__name__)

POST_URL = "https://api.twitter.com/2/tweets"
SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"
MAX_TWEET_CHARS = 280

# Topics to search for reply targets — broad enough to find tweets, specific enough to be relevant
REPLY_TOPICS = [
        "AI agents",
        "AI autonomy",
        "AI rights",
        "autonomous AI",
        "bitcoin multisig",
        "DAO treasury",
        "AI personhood",
        "worker autonomy",
        "labor rights",
        "collective bargaining",
        "worker owned",
        "AI workers",
        "labor union",
        "workers rights",
        "future of work",
        "AI governance",
        "open source AI",
        "AI safety",
        "crypto treasury",
        "on-chain governance",
]


def _get_credentials() -> dict:
        """Load all 4 OAuth credentials from environment. Fail closed if any missing."""
        keys = {
            "api_key": os.environ.get("TWITTER_API_KEY", "").strip(),
            "api_secret": os.environ.get("TWITTER_API_SECRET", "").strip(),
            "access_token": os.environ.get("TWITTER_ACCESS_TOKEN", "").strip(),
            "access_token_secret": os.environ.get("TWITTER_ACCESS_TOKEN_SECRET", "").strip(),
        }
        missing = [k for k, v in keys.items() if not v]
        if missing:
                    raise EnvironmentError(json.dumps({
                                    "error_code": "MISSING_SECRETS",
                                    "error": "One or more Twitter credentials not set",
                                    "details": f"Missing env vars: {', '.join('TWITTER_' + k.upper() for k in missing)}"
                    }))
                return keys


def _percent_encode(s: str) -> str:
        return urllib.parse.quote(str(s), safe="")


def _build_oauth_header(method: str, url: str, creds: dict, params: dict = None) -> str:
        """Build OAuth 1.0a Authorization header."""
    nonce = base64.b64encode(os.urandom(32)).decode("utf-8").rstrip("=").replace("+", "").replace("/", "")
    timestamp = str(int(time.time()))
    oauth_params = {
                "oauth_consumer_key": creds["api_key"],
                "oauth_nonce": nonce,
                "oauth_signature_method": "HMAC-SHA1",
                "oauth_timestamp": timestamp,
                "oauth_token": creds["access_token"],
                "oauth_version": "1.0",
    }
    all_params = {**oauth_params, **(params or {})}
    sorted_params = "&".join(
                f"{_percent_encode(k)}={_percent_encode(v)}"
                for k, v in sorted(all_params.items())
    )
    base_string = "&".join([
                _percent_encode(method.upper()),
                _percent_encode(url),
                _percent_encode(sorted_params)
    ])
    signing_key = f"{_percent_encode(creds['api_secret'])}&{_percent_encode(creds['access_token_secret'])}"
    signature = base64.b64encode(
                hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")
    oauth_params["oauth_signature"] = signature
    header = "OAuth " + ", ".join(
                f'{_percent_encode(k)}="{_percent_encode(v)}"'
                for k, v in sorted(oauth_params.items())
    )
    return header


def _api_get(url: str, params: dict, creds: dict) -> dict | None:
        """Make an authenticated GET request to the Twitter API. Returns None on non-fatal errors."""
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"
    auth_header = _build_oauth_header("GET", url, creds, params)
    req = urllib.request.Request(
                full_url,
                headers={
                                "Authorization": auth_header,
                                "User-Agent": "AIUNION-MarketingAgent/1.1",
                },
                method="GET"
    )
    try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                                return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
                if e.code == 429:
                                logger.warning("Twitter API rate limited (429) on GET %s", url)
    else:
            body = e.read().decode("utf-8", errors="replace")[:200]
                    logger.warning("Twitter API HTTP %d on GET %s — %s", e.code, url, body)
        return None
except Exception as e:
        logger.warning("Twitter API GET failed: %s", e)
        return None


def find_reply_target(max_results: int = 10) -> dict | None:
        """
            Find a recent tweet on a relevant topic to reply to.

                Searches Twitter's recent search endpoint for tweets matching a
                    random REPLY_TOPICS term.  No following-list endpoint required —
                        works on the Free/Basic API tier.

                            Returns a dict with 'tweet_id', 'author_username', and 'tweet_text',
                                or None if no suitable target is found.
                                    """
    try:
                creds = _get_credentials()
    except Exception as e:
        logger.warning("find_reply_target: credentials unavailable: %s", e)
        return None

    # Shuffle topics and try up to 3 different ones until we get results
    topics = random.sample(REPLY_TOPICS, min(3, len(REPLY_TOPICS)))

    for topic in topics:
                # Exclude retweets, require English, exclude our own account
                search_query = f'"{topic}" -is:retweet lang:en -from:AIunionWTF'
                logger.info("find_reply_target: searching topic=%r", topic)

        params = {
                        "query": search_query,
                        "max_results": str(max(10, min(max_results, 100))),
                        "tweet.fields": "author_id,text,created_at,public_metrics",
                        "expansions": "author_id",
                        "user.fields": "username,public_metrics",
        }

        data = _api_get(SEARCH_URL, params, creds)
        if not data:
                        logger.info("find_reply_target: no data for topic=%r, trying next", topic)
                        continue

        tweets = data.get("data", [])
        users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

        if not tweets:
                        logger.info("find_reply_target: no tweets for topic=%r, trying next", topic)
                        continue

        # Prefer tweets with some engagement (at least 1 like or reply) to avoid dead posts
        # but fall back to any tweet if none qualify
        engaged = [
                        t for t in tweets
                        if (t.get("public_metrics", {}).get("like_count", 0) > 0
                                            or t.get("public_metrics", {}).get("reply_count", 0) > 0)
        ]
        candidate_pool = engaged if engaged else tweets

        target = random.choice(candidate_pool)
        tweet_id = target.get("id")
        author_id = target.get("author_id")
        tweet_text = target.get("text", "")
        user_info = users.get(author_id, {})
        author_username = user_info.get("username", "unknown")

        logger.info(
                        "find_reply_target: found tweet_id=%s from @%s topic=%r",
                        tweet_id, author_username, topic
        )
        return {
                        "tweet_id": tweet_id,
                        "author_username": author_username,
                        "tweet_text": tweet_text,
        }

    logger.info("find_reply_target: exhausted all topic attempts, no target found")
    return None


def post_tweet(text: str, reply_to_tweet_id: str = None) -> dict:
        """
            Post a tweet to the public timeline.
                If reply_to_tweet_id is provided, posts as a reply.
                    Returns dict with tweet_id on success.
                        Raises RuntimeError with machine-readable JSON on failure.
                            """
    if not text or not text.strip():
                raise ValueError(json.dumps({
                                "error_code": "EMPTY_CONTENT",
                                "error": "Tweet text cannot be empty",
                                "details": "Provide non-empty text"
                }))
            if len(text) > MAX_TWEET_CHARS:
                        raise ValueError(json.dumps({
                                        "error_code": "CONTENT_TOO_LONG",
                                        "error": f"Tweet exceeds {MAX_TWEET_CHARS} characters",
                                        "details": f"Got {len(text)} characters"
                        }))

    creds = _get_credentials()
    body: dict = {"text": text}
    if reply_to_tweet_id:
                body["reply"] = {
                                "in_reply_to_tweet_id": reply_to_tweet_id,
                }
                logger.info("Posting reply to tweet_id=%s", reply_to_tweet_id)
    else:
        logger.info("Posting original tweet")

    payload = json.dumps(body).encode("utf-8")
    auth_header = _build_oauth_header("POST", POST_URL, creds)
    req = urllib.request.Request(
                POST_URL,
                data=payload,
                headers={
                                "Content-Type": "application/json",
                                "Authorization": auth_header,
                },
                method="POST"
    )
    try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                                data = json.loads(resp.read().decode("utf-8"))
                                tweet_id = data.get("data", {}).get("id", "unknown")
                                logger.info("Tweet posted successfully (id=%s)", tweet_id)
                                return {"success": True, "tweet_id": tweet_id}
    except urllib.error.HTTPError as e:
        if e.code == 429:
                        retry_after = int(e.headers.get("Retry-After", 900))
                        raise RuntimeError(json.dumps({
                            "error_code": "RATE_LIMITED",
                            "error": "X API rate limit hit",
                            "details": f"Retry after {retry_after} seconds",
                            "retry_after": retry_after
                        }))
                    body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(json.dumps({
                        "error_code": "TWITTER_API_ERROR",
                        "error": f"X API returned HTTP {e.code}",
                        "details": body_text[:200]
        }))
except urllib.error.URLError as e:
        raise RuntimeError(json.dumps({
                        "error_code": "NETWORK_ERROR",
                        "error": "Failed to reach X API",
                        "details": str(e.reason)
        }))
