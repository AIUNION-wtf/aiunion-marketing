"""
twitter_client.py
X (Twitter) API v2 posting client for AIUNION marketing agent.
- OAuth 1.0a (required for posting on free/pay-per-use tier)
- Write-only: posts to public timeline only, no DM, no read
- Fails closed if any required secret is missing
- Returns machine-readable errors
- Includes Retry-After on 429 responses
- find_reply_target() dynamically fetches followed accounts and searches their recent tweets
"""
import os
import json
import time
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
import urllib.parse
import logging

logger = logging.getLogger(__name__)

POST_URL = "https://api.twitter.com/2/tweets"
SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"
USERS_URL = "https://api.twitter.com/2/users"
MAX_TWEET_CHARS = 280

# AIUNION's own Twitter user ID (fixed — does not change)
AIUNION_USER_ID = "1889813178207100929"

# Topics to search for within followed accounts' tweets
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
            logger.warning("Twitter API HTTP %d on GET %s", e.code, url)
        return None
    except Exception as e:
        logger.warning("Twitter API GET failed: %s", e)
        return None


def get_following_usernames(max_results: int = 100) -> list[str]:
    """
    Fetch the list of usernames AIUNION is currently following via Twitter API.
    Returns a list of username strings (without @).
    Falls back to empty list on any error — caller handles gracefully.
    """
    try:
        creds = _get_credentials()
    except Exception as e:
        logger.warning("get_following_usernames: credentials unavailable: %s", e)
        return []

    url = f"{USERS_URL}/{AIUNION_USER_ID}/following"
    params = {
        "max_results": str(min(max_results, 1000)),
        "user.fields": "username",
    }
    data = _api_get(url, params, creds)
    if not data:
        logger.warning("get_following_usernames: no data returned")
        return []

    users = data.get("data", [])
    usernames = [u["username"] for u in users if u.get("username")]
    logger.info("get_following_usernames: fetched %d followed accounts", len(usernames))
    return usernames


def find_reply_target(query: str = None, max_results: int = 10) -> dict | None:
    """
    Find a recent tweet from a followed account on a relevant topic to reply to.

    Dynamically fetches AIUNION's following list and builds a search query
    targeting those accounts on AI/Bitcoin topics. This means as new accounts
    are followed, they are automatically included with no code changes needed.

    Returns a dict with 'tweet_id', 'author_username', and 'tweet_text',
    or None if no suitable target found.
    """
    try:
        creds = _get_credentials()
    except Exception as e:
        logger.warning("find_reply_target: credentials unavailable: %s", e)
        return None

    # Fetch who we're following
    following = get_following_usernames(max_results=100)

    if not following:
        logger.info("find_reply_target: no followed accounts found, skipping reply")
        return None

    # Build from: filter — Twitter search supports up to ~25 OR'd from: clauses efficiently
    # Pick a random sample of up to 20 accounts to keep the query within limits
    import random
    sample = random.sample(following, min(20, len(following)))
    from_clause = " OR ".join(f"from:{u}" for u in sample)

    # Pick a random topic to search for
    topic = random.choice(REPLY_TOPICS)

    # Full query: topic + from one of our followed accounts + exclude retweets
    search_query = f'({topic}) ({from_clause}) -is:retweet lang:en'
    logger.info("find_reply_target: searching with topic=%r sample_size=%d", topic, len(sample))

    params = {
        "query": search_query,
        "max_results": str(max_results),
        "tweet.fields": "author_id,text,created_at",
        "expansions": "author_id",
        "user.fields": "username",
    }
    data = _api_get(SEARCH_URL, params, creds)
    if not data:
        logger.info("find_reply_target: search returned no data")
        return None

    tweets = data.get("data", [])
    users = {u["id"]: u["username"] for u in data.get("includes", {}).get("users", [])}

    if not tweets:
        logger.info("find_reply_target: no tweets found for topic=%r", topic)
        return None

    # Pick the most recent tweet
    target = tweets[0]
    tweet_id = target.get("id")
    author_id = target.get("author_id")
    tweet_text = target.get("text", "")
    author_username = users.get(author_id, "unknown")

    logger.info(
        "find_reply_target: found tweet_id=%s from @%s topic=%r",
        tweet_id, author_username, topic
    )
    return {
        "tweet_id": tweet_id,
        "author_username": author_username,
        "tweet_text": tweet_text,
    }


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
            "auto_populate_reply_metadata": True,
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
