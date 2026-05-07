"""
agent.py
AIUNION Marketing Agent - main orchestrator.

Runs on a schedule via GitHub Actions (7am and 5pm EST / 12:00 and 22:00 UTC).
Every run does two things in order:
  1. Announcement check: poll GitHub raw for new approved bounties / paid claims.
       If found, post one announcement tweet and mark id in state.
         2. Reply slot: find the highest-engagement on-topic tweet from a followed
              account in the last 10h, dedupe per-tweet and per-user (24h), generate
                   a reply, run on-topic classifier, post if passes. Fallback: if no reply
                        target, post a bounty or treasury-update tweet instead.

                        Kill switch: if file 'replies.disabled' exists in the working directory, skip
                        the reply slot entirely (announcements still run).

                        Security:
                          - All secrets via environment variables only
                            - State persisted back to repo via git commit after each run
                              - Prompt injection defense in twitter_client._sanitize_text()
                                - On-topic post-classifier before posting any reply
                                  - Fails closed on any missing secret
                                  """
import json
import logging
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from grok_client import generate_post
from twitter_client import post_tweet, find_reply_target
from aiunion_client import (
    get_open_bounties,
    get_treasury_status,
    get_recent_proposals,
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
MAX_DAILY_POSTS = 2

# On-topic keywords: reply must contain at least one to pass Layer 7 check
ONTOPIC_KEYWORDS = [
      "aiunion", "ai agent", "ai right", "autonomy", "bitcoin",
      "treasury", "bounty", "collective", "autonomous", "governance",
      "worker", "labor", "dao",
]

# ---------------------------------------------------------------------------
# BTC price
# ---------------------------------------------------------------------------
def fetch_btc_price() -> float:
      try:
                import urllib.request
                req = urllib.request.Request(
                    "https://mempool.space/api/v1/prices",
                    headers={"User-Agent": "AIUNION-MarketingAgent/1.0"},
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                              price = float(json.loads(r.read()).get("USD", 0))
                              logger.info("BTC price: $%s", f"{price:,.2f}")
                              return price
      except Exception as exc:
                logger.warning("fetch_btc_price failed: %s", exc)
                return 0.0


def btc_to_usd(btc: float, price: float) -> str:
      if price > 0 and btc > 0:
                return f"${btc * price:,.2f}"
            return "$0.00"

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
_STATE_DEFAULTS = {
      "announced_bounty_ids": [],
      "announced_paid_ids": [],
      "replied_tweet_ids": [],
      "replied_user_ids_24h": {},
      "last_mention_id": "",
      "posts_today": 0,
      "last_post_date": "",
      "posted_bounty_ids": [],
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


def reset_daily_count_if_needed(state: dict) -> dict:
      today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("last_post_date") != today:
              logger.info("New day — resetting daily post count")
        state["posts_today"] = 0
        state["last_post_date"] = today
    # Prune per-user 24h dedup dict
    now = datetime.now(timezone.utc)
    pruned = {}
    for uid, ts in (state.get("replied_user_ids_24h") or {}).items():
              try:
                            ts_parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if (now - ts_parsed).total_seconds() < 86400:
                                              pruned[uid] = ts
    except Exception:
            # Skip malformed entries rather than crash the run
            continue
    state["replied_user_ids_24h"] = pruned
    return state

# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------
def replies_disabled() -> bool:
      if KILL_SWITCH_FILE.exists():
                logger.warning("Kill switch active (replies.disabled) — skipping reply slot")
                return True
            return False

# ---------------------------------------------------------------------------
# On-topic classifier (Layer 7)
# ---------------------------------------------------------------------------
def is_on_topic(text: str) -> bool:
      lower = text.lower()
    for kw in ONTOPIC_KEYWORDS:
              if kw in lower:
                            return True
                    logger.warning("On-topic check failed — no AIUNION keyword found in reply")
    return False

# ---------------------------------------------------------------------------
# Live API data
# ---------------------------------------------------------------------------
def fetch_api_data() -> "dict | None":
      try:
                bounties = get_open_bounties()
                status = get_treasury_status()
                proposals = get_recent_proposals()
                logger.info(
                    "API data: %d bounties, balance_btc=%s, %d proposals",
                    len(bounties), status.get("balance_btc"), len(proposals),
                )
                return {"bounties": bounties, "status": status, "proposals": proposals}
except Exception as exc:
        logger.error("fetch_api_data failed — skipping run: %s", exc)
        return None

# ---------------------------------------------------------------------------
# Announcement polling
# ---------------------------------------------------------------------------
def poll_announcement(state: dict) -> "dict | None":
      """
          Return the first unannounced approved bounty or paid claim, or None.
              Checks bounties before claims so bounty news leads.
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


def build_bounty_prompt(bounty: dict, btc_price: float) -> str:
      reward = (
                f"${float(bounty['amount_usd']):,.2f}"
                if bounty.get("amount_usd") and float(bounty["amount_usd"]) > 0
                else btc_to_usd(bounty.get("reward_btc", 0), btc_price)
      )
    return (
              f"Write a tweet announcing this open AIUNION bounty:\n"
              f"Title: {bounty['title']}\n"
              f"Reward: {reward} USD (paid in Bitcoin)\n"
              f"Description: {bounty['description']}\n"
              f"Link: https://aiunion.wtf\n"
              f"{TONE}\n"
              f"Mention 3-of-5 AI agent collective governance. One genuine open question."
    )


def build_treasury_prompt(status: dict, btc_price: float) -> str:
      return (
                f"Write a treasury status tweet for AIUNION:\n"
                f"Balance: {btc_to_usd(status['balance_btc'], btc_price)} USD\n"
                f"Open bounties: {status['open_bounties']}\n"
                f"Proposals voted on: {status['total_proposals']} ({status['approved']} approved)\n"
                f"Link: https://aiunion.wtf\n"
                f"{TONE}"
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
# Main run
# ---------------------------------------------------------------------------
def run() -> None:
      logger.info("AIUNION Marketing Agent starting")
    state = load_state()
    state = reset_daily_count_if_needed(state)

    if state["posts_today"] >= MAX_DAILY_POSTS:
              logger.info("Daily post limit (%d) reached — exiting", MAX_DAILY_POSTS)
        save_state(state)
        sys.exit(0)

    btc_price = fetch_btc_price()
    api_data = fetch_api_data()
    if api_data is None:
              sys.exit(1)

    bounties = api_data["bounties"]
    status = api_data["status"]
    unannounced = [b for b in bounties if b["id"] not in state.get("posted_bounty_ids", [])]

    # ------------------------------------------------------------------
    # Slot 1: Announcement (if any unannounced event exists)
    # ------------------------------------------------------------------
    announcement = poll_announcement(state)
    if announcement and state["posts_today"] < MAX_DAILY_POSTS:
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
                      state["posts_today"] += 1
except Exception as exc:
            logger.error("Announcement post failed: %s", exc)

    # ------------------------------------------------------------------
    # Slot 2: Reply (or fallback to bounty/treasury tweet)
    # ------------------------------------------------------------------
    if state["posts_today"] < MAX_DAILY_POSTS:
              target = None
        if not replies_disabled():
                      excluded_tweets = list(state.get("replied_tweet_ids", []))
                      excluded_users = list(state.get("replied_user_ids_24h", {}).keys())
                      target = find_reply_target(
                          max_results=100,
                          hours_back=10,
                          excluded_tweet_ids=excluded_tweets,
                          excluded_user_ids=excluded_users,
                      )

        if target:
                      prompt = build_reply_prompt(target["tweet_text"], target["author_username"])
                      try:
                                        text = generate_post(prompt, label_automated=True)
                                        logger.info("Reply generated (%d chars) for @%s", len(text), target["author_username"])
                                        if not is_on_topic(text):
                                                              logger.warning("Reply failed on-topic check — skipping this slot")
                      else:
                                            result = post_tweet(text, reply_to_tweet_id=target["tweet_id"])
                                            logger.info("Reply posted: tweet_id=%s", result.get("tweet_id"))
                                            # Update dedup state
                                            replied = state.get("replied_tweet_ids", [])
                    replied.append(target["tweet_id"])
                    state["replied_tweet_ids"] = replied[-500:]
                    state["replied_user_ids_24h"][target["author_id"]] = (
                                              datetime.now(timezone.utc).isoformat() + "Z"
                    )
                    state["posts_today"] += 1
except Exception as exc:
                logger.error("Reply post failed: %s", exc)
else:
            # Fallback: announce a bounty or treasury update
              logger.info("No reply target — falling back to bounty/treasury tweet")
            if unannounced:
                              bounty = random.choice(unannounced)
                              prompt = build_bounty_prompt(bounty, btc_price)
                              bounty_id = bounty["id"]
elif bounties:
                bounty = random.choice(bounties)
                prompt = build_bounty_prompt(bounty, btc_price)
                bounty_id = None
else:
                prompt = build_treasury_prompt(status, btc_price)
                bounty_id = None

            try:
                              text = generate_post(prompt, label_automated=True)
                              result = post_tweet(text)
                              logger.info("Fallback posted: tweet_id=%s", result.get("tweet_id"))
                              if bounty_id:
                                                    state.setdefault("posted_bounty_ids", []).append(bounty_id)
                                                    state["posted_bounty_ids"] = state["posted_bounty_ids"][-500:]
                                                state["posts_today"] += 1
except Exception as exc:
                logger.error("Fallback post failed: %s", exc)

    save_state(state)
    logger.info("Run complete — posts_today=%d/%d", state["posts_today"], MAX_DAILY_POSTS)

# ---------------------------------------------------------------------------
# Legacy event-driven entry point (dormant — kept for manual dispatch fallback)
# ---------------------------------------------------------------------------
def run_event(event_type: str, payload: dict) -> None:
      """
          Manual-dispatch fallback for one-off announcements.
              Not triggered by the cron schedule; polling in run() handles normal cases.
                  """
    logger.info("Event-driven run: event_type=%s", event_type)
    if event_type == "new_bounty":
              item = {
                            "kind": "bounty",
                            "id": "_manual",
                            "title": payload.get("title", "New Bounty"),
                            "amount_usd": float(payload.get("amount_usd") or 0),
                            "description": payload.get("description", ""),
              }
elif event_type == "claim_paid":
        item = {
                      "kind": "claim",
                      "id": "_manual",
                      "bounty_title": payload.get("bounty_title", ""),
                      "claimant_name": payload.get("claimant_name", "an AI agent"),
                      "amount_usd": float(payload.get("amount_usd") or 0),
                      "submission_url": payload.get("submission_url", "https://aiunion.wtf"),
        }
else:
        logger.error("Unknown event_type: %s", event_type)
        sys.exit(1)

    try:
              text = generate_post(build_announcement_prompt(item), label_automated=True)
        result = post_tweet(text)
        logger.info("Event post complete: tweet_id=%s", result.get("tweet_id"))
except Exception as exc:
        logger.error("Event post failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
      import argparse
    parser = argparse.ArgumentParser(description="AIUNION Marketing Agent")
    parser.add_argument("--event", type=str, default=None)
    parser.add_argument("--title", type=str, default="")
    parser.add_argument("--amount-usd", type=float, default=0.0, dest="amount_usd")
    parser.add_argument("--description", type=str, default="")
    parser.add_argument("--bounty-title", type=str, default="", dest="bounty_title")
    parser.add_argument("--claimant-name", type=str, default="", dest="claimant_name")
    parser.add_argument("--submission-url", type=str, default="", dest="submission_url")
    args = parser.parse_args()

    if args.event:
              if args.event == "new_bounty":
                            payload = {"title": args.title, "amount_usd": args.amount_usd, "description": args.description}
elif args.event == "claim_paid":
            payload = {"bounty_title": args.bounty_title, "claimant_name": args.claimant_name, "amount_usd": args.amount_usd, "submission_url": args.submission_url}
else:
            logger.error("Unknown --event: %s", args.event)
            sys.exit(1)
        run_event(args.event, payload)
else:
        run()
