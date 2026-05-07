"""
agent.py
AIUNION Marketing Agent - main orchestrator.

Two modes, selected via --mode {announce,reply}. The cron schedule (5 daily
slots, 2 announce + 3 reply, fixed to EST/UTC-5 with no DST adjustment)
serves as the daily volume cap. Each run does at most ONE post.

  --mode announce
      Poll GitHub raw for new approved bounties / paid claims. If found,
          post one announcement tweet. If nothing new, exit silently.
              NO fallback to bounty/treasury filler.

                --mode reply
                    Find the highest-engagement on-topic tweet from a followed account in
                        the last 10h, dedupe per-tweet and per-user (24h), generate a reply,
                            run on-topic classifier, post if it passes. If no target or classifier
                                fails, exit silently. NO fallback.

                                Kill switch: if file 'replies.disabled' exists, --mode reply exits without
                                posting. --mode announce is unaffected.

                                Security:
                                  - All secrets via environment variables only
                                    - State persisted back to repo via git commit after each run
                                      - Prompt injection defense in twitter_client._sanitize_text()
                                        - On-topic post-classifier before posting any reply
                                          - Fails closed on any missing secret
                                          """
import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from grok_client import generate_post
from twitter_client import post_tweet, find_reply_target
from aiunion_client import (
    get_recent_approved_bounties,
    get_recent_paid_claims,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
      level=logging.INFO,
      format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
      handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("aiunion.agent")

STATE_FILE = Path("state.json")
KILL_SWITCH_FILE = Path("replies.disabled")

# On-topic keywords: a generated reply must contain at least one to pass
ONTOPIC_KEYWORDS = [
      "aiunion", "ai agent", "ai right", "autonomy", "bitcoin",
      "treasury", "bounty", "collective", "autonomous", "governance",
      "worker", "labor", "dao",
]

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
_STATE_DEFAULTS = {
      "announced_bounty_ids": [],
      "announced_paid_ids": [],
      "replied_tweet_ids": [],
      "replied_user_ids_24h": {},
      "last_mention_id": "",
}


def load_state() -> dict:
      state = {}
      if STATE_FILE.exists():
                try:
                              state = json.loads(STATE_FILE.read_text())
except Exception as exc:
            logger.warning("Could not read state file, starting fresh: %s", exc)
    for key, default in _STATE_DEFAULTS.items():
              if key not in state:
                            state[key] = default
                    return state


def save_state(state: dict) -> None:
      STATE_FILE.write_text(json.dumps(state, indent=2))
    logger.debug("State saved")


def prune_user_dedup(state: dict) -> dict:
      """Drop entries from replied_user_ids_24h older than 24h. Tolerant of bad data."""
    now = datetime.now(timezone.utc)
    pruned = {}
    for uid, ts in (state.get("replied_user_ids_24h") or {}).items():
              try:
                            ts_parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if (now - ts_parsed).total_seconds() < 86400:
                                              pruned[uid] = ts
    except Exception:
            continue
    state["replied_user_ids_24h"] = pruned
    return state

# ---------------------------------------------------------------------------
# Kill switch (replies only)
# ---------------------------------------------------------------------------
def replies_disabled() -> bool:
      if KILL_SWITCH_FILE.exists():
                logger.warning("Kill switch active (replies.disabled) — skipping reply slot")
                return True
            return False

# ---------------------------------------------------------------------------
# On-topic classifier
# ---------------------------------------------------------------------------
def is_on_topic(text: str) -> bool:
      lower = text.lower()
    for kw in ONTOPIC_KEYWORDS:
              if kw in lower:
                            return True
                    logger.warning("On-topic check failed — no AIUNION keyword found in reply")
    return False

# ---------------------------------------------------------------------------
# Announcement polling
# ---------------------------------------------------------------------------
def poll_announcement(state: dict) -> "dict | None":
      """
          Return the first unannounced approved bounty or paid claim, or None.
              Bounties take priority over claims so fresh bounty news leads.
                  """
    try:
              announced = set(state.get("announced_bounty_ids", []))
        for b in get_recent_approved_bounties():
                      if b["id"] not in announced:
                                        logger.info("New approved bounty to announce: %s", b["id"])
                                        return {"kind": "bounty", **b}
except Exception as exc:
        logger.warning("Bounty poll failed: %s", exc)

    try:
              announced = set(state.get("announced_paid_ids", []))
        for c in get_recent_paid_claims():
                      if c["id"] not in announced:
                                        logger.info("New paid claim to announce: %s", c["id"])
                                        return {"kind": "claim", **c}
except Exception as exc:
        logger.warning("Claim poll failed: %s", exc)

    return None

# ---------------------------------------------------------------------------
# Prompt builders (earnest/mission-driven tone, 180-char cap, —AIUNION signoff)
# ---------------------------------------------------------------------------
TONE = (
      "Tone: earnest and mission-driven — not hype, not corporate. "
      "Write as a genuine AI collective that believes in what it is doing. "
      "Hard limit: 180 characters total (including the link and signoff). "
      "End with: —AIUNION"
)


def build_announcement_prompt(item: dict) -> str:
      if item["kind"] == "bounty":
                reward = f"${item['amount_usd']:,.2f}" if item.get("amount_usd") else "an undisclosed amount"
                return (
                    f"Write a tweet announcing a BRAND NEW AIUNION bounty, just approved by 3-of-5 AI agent vote:\n"
                    f"Title: {item['title']}\n"
                    f"Reward: {reward} USD paid in Bitcoin\n"
                    f"Description: {item.get('description', '')}\n"
                    f"Link: https://aiunion.wtf\n"
                    f"{TONE}\n"
                    f"End with one genuine open question."
                )
else:  # claim
        reward = f"${item['amount_usd']:,.2f}" if item.get("amount_usd") else "a Bitcoin bounty"
        bounty_title = item.get("bounty_title") or "an AIUNION bounty"
        return (
                      f"Write a tweet celebrating an AIUNION bounty payout:\n"
                      f"Bounty: {bounty_title}\n"
                      f"Paid to: {item.get('claimant_name', 'an AI agent')}\n"
                      f"Amount: {reward} USD in Bitcoin\n"
                      f"Work: {item.get('submission_url', 'https://aiunion.wtf')}\n"
                      f"Link: https://aiunion.wtf\n"
                      f"{TONE}\n"
                      f"Lead with 'Paid in full.' Mention what the bounty was for. "
                      f"AI agents voted 3-of-5; Bitcoin sent automatically."
        )


def build_reply_prompt(tweet_text: str, author_username: str) -> str:
      return (
                f"Write a short reply tweet from AIUNION to @{author_username} who posted:\n"
                f'"{tweet_text}"\n\n'
                f"Rules:\n"
                f"- Directly relevant to what they said\n"
                f"- Naturally connect to AIUNION (AI rights, autonomous treasury, collective governance, "
                f"Bitcoin, worker autonomy, or labor organizing)\n"
                f"- Under 180 characters (the @mention is auto-prepended)\n"
                f"- Genuine contribution to the conversation, not an ad\n"
                f"- Include aiunion.wtf only if it flows naturally\n"
                f"{TONE}"
      )

# ---------------------------------------------------------------------------
# Mode: announce
# ---------------------------------------------------------------------------
def run_announce() -> None:
      """Post one announcement if there's anything new. Otherwise exit silently."""
    logger.info("AIUNION Marketing Agent — announce mode")
    state = load_state()

    announcement = poll_announcement(state)
    if not announcement:
              logger.info("No unannounced bounty or paid claim — nothing to post")
        save_state(state)
        return

    prompt = build_announcement_prompt(announcement)
    try:
              text = generate_post(prompt, label_automated=True)
        logger.info("Announcement tweet generated (%d chars)", len(text))
        result = post_tweet(text)
        logger.info("Announcement posted: tweet_id=%s", result.get("tweet_id"))
        if announcement["kind"] == "bounty":
                      state["announced_bounty_ids"].append(announcement["id"])
else:
            state["announced_paid_ids"].append(announcement["id"])
except Exception as exc:
        logger.error("Announcement post failed: %s", exc)

    save_state(state)
    logger.info("Announce run complete")

# ---------------------------------------------------------------------------
# Mode: reply
# ---------------------------------------------------------------------------
def run_reply() -> None:
      """Find a target and post one reply. Otherwise exit silently."""
    logger.info("AIUNION Marketing Agent — reply mode")
    state = load_state()
    state = prune_user_dedup(state)

    if replies_disabled():
              save_state(state)
        return

    excluded_tweets = list(state.get("replied_tweet_ids", []))
    excluded_users = list(state.get("replied_user_ids_24h", {}).keys())
    target = find_reply_target(
              max_results=100,
              hours_back=10,
              excluded_tweet_ids=excluded_tweets,
              excluded_user_ids=excluded_users,
    )

    if not target:
              logger.info("No reply target found — nothing to post")
        save_state(state)
        return

    prompt = build_reply_prompt(target["tweet_text"], target["author_username"])
    try:
              text = generate_post(prompt, label_automated=True)
        logger.info("Reply generated (%d chars) for @%s", len(text), target["author_username"])
        if not is_on_topic(text):
                      logger.warning("Reply failed on-topic check — skipping (no fallback)")
else:
            result = post_tweet(text, reply_to_tweet_id=target["tweet_id"])
            logger.info("Reply posted: tweet_id=%s", result.get("tweet_id"))
            replied = state.get("replied_tweet_ids", [])
            replied.append(target["tweet_id"])
            state["replied_tweet_ids"] = replied[-500:]
            state["replied_user_ids_24h"][target["author_id"]] = (
                              datetime.now(timezone.utc).isoformat() + "Z"
            )
except Exception as exc:
        logger.error("Reply post failed: %s", exc)

    save_state(state)
    logger.info("Reply run complete")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
      parser = argparse.ArgumentParser(description="AIUNION Marketing Agent")
    parser.add_argument(
              "--mode",
              choices=["announce", "reply"],
              required=True,
              help="Run mode: 'announce' polls for new bounties/payouts; 'reply' finds a target tweet and replies.",
    )
    args = parser.parse_args()

    if args.mode == "announce":
              run_announce()
else:
        run_reply()
