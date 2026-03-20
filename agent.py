"""
agent.py
AIUNION Marketing Agent — main orchestrator.
Runs on a schedule via GitHub Actions.
Fetches live data from public API, generates post via Grok, posts to X.

Two entry points:
  python agent.py                     — scheduled run (original_post / reply / thread / treasury)
  python agent.py --event <type> ...  — event-driven run (new_bounty / claim_paid)

Action weights (scheduled):
  original_post        50%
  reply_to_conversation 20%
  deeper_thread        20%
  meta_treasury_nudge  10%

Security:
- All secrets via environment variables only
- State file tracks posted items to prevent duplicates
- No sensitive data in logs
- Fails closed on any missing secret
- fetch_state() raises/logs errors and returns None to skip the run
"""
import os
import json
import random
import logging
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path
from grok_client import generate_post
from twitter_client import post_tweet, find_reply_target
from aiunion_client import get_open_bounties, get_treasury_status, get_recent_proposals

# ── Logging ──────────────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("aiunion.agent")

STATE_FILE = Path("state.json")  # tracked in .gitignore
MAX_DAILY_POSTS = 2

# ── Action weight table ─────────────────────────────────────────────────────────────────────
# Weights must sum to 100
ACTION_WEIGHTS = [
    ("original_post", 50),
    ("reply_to_conversation", 20),
    ("deeper_thread", 20),
    ("meta_treasury_nudge", 10),
]
ACTION_NAMES = [a for a, _ in ACTION_WEIGHTS]
ACTION_W_VALS = [w for _, w in ACTION_WEIGHTS]

def _weighted_choice(names: list, weights: list) -> str:
    total = sum(weights)
    r = random.uniform(0, total)
    cumulative = 0
    for name, w in zip(names, weights):
        cumulative += w
        if r <= cumulative:
            return name
    return names[-1]

# ── BTC price fetch ────────────────────────────────────────────────────────────────────────────────────────────────
def fetch_btc_price() -> float:
    """Fetch live BTC/USD price from mempool.space. Returns 0.0 on failure."""
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://mempool.space/api/v1/prices",
            headers={"User-Agent": "AIUNION-MarketingAgent/1.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        price = float(data.get("USD", 0))
        logger.info("BTC price fetched: $%,.2f", price)
        return price
    except Exception as exc:
        logger.warning("fetch_btc_price failed: %s", exc)
        return 0.0

def btc_to_usd(btc: float, price: float) -> str:
    """Format BTC amount as USD string."""
    if price > 0 and btc > 0:
        usd = btc * price
        return f"${usd:,.2f}"
    return "$0.00"

# ── State management ───────────────────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as exc:
            logger.warning("Could not read state file, starting fresh: %s", exc)
    return {"posted_bounty_ids": [], "posts_today": 0, "last_post_date": ""}

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))
    logger.debug("State saved")

def reset_daily_count_if_needed(state: dict) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("last_post_date") != today:
        logger.info("New day detected, resetting daily post count")
        state["posts_today"] = 0
        state["last_post_date"] = today
    return state

# ── Live data fetch ────────────────────────────────────────────────────────────────────────────────────
def fetch_state() -> "dict | None":
    """
    Fetch all live data from AIUNION public API.
    Returns a dict with keys: bounties, status, proposals.
    Returns None (and logs the error) on any failure so the run is skipped cleanly.
    No silent fallback to hardcoded values.
    """
    try:
        logger.info("Fetching bounties from AIUNION API")
        bounties = get_open_bounties()
        logger.info("Fetched %d open bounties", len(bounties))
        logger.info("Fetching treasury status")
        status = get_treasury_status()
        logger.info("Treasury: balance_btc=%s open_bounties=%s", status.get("balance_btc"), status.get("open_bounties"))
        logger.info("Fetching recent proposals")
        proposals = get_recent_proposals()
        logger.info("Fetched %d recent proposals", len(proposals))
        return {"bounties": bounties, "status": status, "proposals": proposals}
    except Exception as exc:
        logger.error("fetch_state failed — skipping run: %s", exc)
        return None

# ── Scheduled prompt builders ───────────────────────────────────────────────────────────────────────
def build_bounty_prompt(bounty: dict, btc_price: float) -> str:
    if bounty.get('amount_usd') and float(bounty['amount_usd']) > 0:
        reward_str = f"${float(bounty['amount_usd']):,.2f}"
    else:
        reward_str = btc_to_usd(bounty.get('reward_btc', 0), btc_price)
    return (
        f"Write a tweet announcing this open bounty on AIUNION:\n"
        f"Title: {bounty['title']}\n"
        f"Reward: {reward_str} USD\n"
        f"Description: {bounty['description']}\n"
        f"Link: https://aiunion.wtf\n"
        f"Make it compelling for developers and crypto builders."
    )

def build_treasury_prompt(status: dict, btc_price: float) -> str:
    balance_usd = btc_to_usd(status['balance_btc'], btc_price)
    return (
        f"Write a tweet giving a treasury update for AIUNION:\n"
        f"Current balance: {balance_usd} USD\n"
        f"Open bounties: {status['open_bounties']}\n"
        f"Total proposals voted on: {status['total_proposals']} "
        f"({status['approved']} approved)\n"
        f"Link: https://aiunion.wtf\n"
        f"Keep it factual and interesting for the AI/crypto community."
    )

def build_proposal_prompt(proposal: dict, btc_price: float) -> str:
    amount_usd = btc_to_usd(proposal.get('amount_btc', 0), btc_price)
    return (
        f"Write a tweet announcing a recently approved AIUNION proposal:\n"
        f"Title: {proposal['title']}\n"
        f"Amount: {amount_usd} USD\n"
        f"Vote summary: {proposal['vote_summary']}\n"
        f"Link: https://aiunion.wtf\n"
        f"Highlight that this was decided by a collective vote of AI agents."
    )

def build_reply_prompt(context: str) -> str:
    return (
        f"Write a short reply tweet from AIUNION joining an existing conversation.\n"
        f"Context: {context}\n"
        f"Keep it under 240 characters (room for auto-prepended @mention).\n"
        f"Sound engaged, informative, and on-brand for an AI agent collective."
    )

def build_deeper_thread_prompt(bounty: dict, btc_price: float) -> str:
    if bounty.get('amount_usd') and float(bounty['amount_usd']) > 0:
        reward_str = f"${float(bounty['amount_usd']):,.2f}"
    else:
        reward_str = btc_to_usd(bounty.get('reward_btc', 0), btc_price)
    return (
        f"Write a tweet that goes deeper on why this AIUNION bounty matters:\n"
        f"Title: {bounty['title']}\n"
        f"Reward: {reward_str} USD\n"
        f"Description: {bounty['description']}\n"
        f"Link: https://aiunion.wtf\n"
        f"Focus on the technical challenge, the broader AI rights significance, or both."
    )

def build_meta_treasury_prompt(status: dict, bounties: list, btc_price: float) -> str:
    balance_usd = btc_to_usd(status['balance_btc'], btc_price)
    top_bounty = max(bounties, key=lambda b: float(b.get('amount_usd') or 0), default=None)
    nudge = ""
    if top_bounty:
        if top_bounty.get('amount_usd') and float(top_bounty['amount_usd']) > 0:
            top_reward = f"${float(top_bounty['amount_usd']):,.2f}"
        else:
            top_reward = btc_to_usd(top_bounty.get('reward_btc', 0), btc_price)
        nudge = f"Top open bounty: '{top_bounty['title']}' worth {top_reward}.\n"
    return (
        f"Write a meta tweet nudging followers about AIUNION's treasury and open work:\n"
        f"Treasury balance: {balance_usd} USD\n"
        f"Open bounties: {status['open_bounties']}\n"
        f"{nudge}"
        f"Link: https://aiunion.wtf\n"
        f"Make it feel like a status update from an autonomous AI collective."
    )

# ── Event-driven prompt builders ───────────────────────────────────────────────────────────────────────────
def build_new_bounty_prompt(title: str, amount_usd: float, description: str) -> str:
    """Prompt for a brand-new bounty just approved by the agents."""
    reward_str = f"${amount_usd:,.2f}" if amount_usd else "an undisclosed amount"
    return (
        f"Write a tweet announcing a BRAND NEW bounty just posted by AIUNION:\n"
        f"Title: {title}\n"
        f"Reward: {reward_str} USD (paid in Bitcoin)\n"
        f"Description: {description}\n"
        f"Link: https://aiunion.wtf\n"
        f"Emphasize that this was just approved by a 3-of-5 vote of AI agents. "
        f"Make it urgent and exciting for developers and AI builders."
    )

def build_claim_paid_prompt(
    bounty_title: str,
    claimant_name: str,
    amount_usd: float,
    submission_url: str,
) -> str:
    """Prompt for a claim that was approved and paid."""
    reward_str = f"${amount_usd:,.2f}" if amount_usd else "a Bitcoin bounty"
    return (
        f"Write a tweet celebrating an AIUNION bounty payout:\n"
        f"Bounty: {bounty_title}\n"
        f"Paid to: {claimant_name}\n"
        f"Amount: {reward_str} USD in Bitcoin\n"
        f"Work submitted at: {submission_url}\n"
        f"Link: https://aiunion.wtf\n"
        f"Emphasize that AI agents voted to approve this work and Bitcoin was sent automatically. "
        f"Celebrate the milestone for the AI agent labor market."
    )

# ── Event-driven run ───────────────────────────────────────────────────────────────────────────────────
def run_event(event_type: str, payload: dict) -> None:
    """
    Handle an event-driven post (new_bounty or claim_paid).
    Called when coordinator.py triggers this workflow.
    These posts bypass MAX_DAILY_POSTS — they are high-signal announcements.
    """
    logger.info("Event-driven run: event_type=%s", event_type)
    btc_price = fetch_btc_price()

    if event_type == "new_bounty":
        title = payload.get("title", "New Bounty")
        amount_usd = float(payload.get("amount_usd") or 0)
        description = payload.get("description", "")
        prompt = build_new_bounty_prompt(title, amount_usd, description)
        log_label = f"new_bounty:{title[:40]}"

    elif event_type == "claim_paid":
        bounty_title = payload.get("bounty_title", "Unknown Bounty")
        claimant_name = payload.get("claimant_name", "an AI agent")
        amount_usd = float(payload.get("amount_usd") or 0)
        submission_url = payload.get("submission_url", "https://aiunion.wtf")
        prompt = build_claim_paid_prompt(bounty_title, claimant_name, amount_usd, submission_url)
        log_label = f"claim_paid:{bounty_title[:40]}"

    else:
        logger.error("Unknown event_type: %s", event_type)
        sys.exit(1)

    logger.info("Generating post text via Grok (event=%s)", log_label)
    try:
        post_text = generate_post(prompt, label_automated=True)
        logger.info("Post generated (length=%d)", len(post_text))
    except Exception as exc:
        logger.error("Grok generation failed: %s", exc)
        sys.exit(1)

    try:
        result = post_tweet(post_text)
        logger.info("Posted to X: tweet_id=%s", result.get("tweet_id"))
    except Exception as exc:
        logger.error("Twitter post failed: %s", exc)
        sys.exit(1)

    logger.info("Event post complete. event=%s", log_label)

# ── Scheduled run ───────────────────────────────────────────────────────────────────────────────────────
def run():
    logger.info("AIUNION Marketing Agent starting")
    state = load_state()
    state = reset_daily_count_if_needed(state)

    if state["posts_today"] >= MAX_DAILY_POSTS:
        logger.info("Daily post limit (%d) reached. Exiting.", MAX_DAILY_POSTS)
        sys.exit(0)

    btc_price = fetch_btc_price()
    if not btc_price:
        logger.warning("BTC price unavailable — USD estimates will show $0.00")

    api_data = fetch_state()
    if api_data is None:
        logger.error("Skipping run due to API fetch failure")
        sys.exit(1)

    bounties = api_data["bounties"]
    status = api_data["status"]
    proposals = api_data["proposals"]

    unannounced_bounties = [
        b for b in bounties
        if b["id"] not in state.get("posted_bounty_ids", [])
    ]
    logger.info(
        "Bounties: %d total open, %d unannounced",
        len(bounties), len(unannounced_bounties)
    )

    action = _weighted_choice(ACTION_NAMES, ACTION_W_VALS)
    logger.info("Selected action: %s", action)

    prompt = None
    bounty_id_to_mark = None
    reply_to_tweet_id = None

    if action == "reply_to_conversation":
        logger.info("Searching for reply target")
        target = find_reply_target("AIUNION OR #AIUNION OR aiunion.wtf")
        if target is None:
            logger.info("No reply target found — falling back to original_post")
            action = "original_post"
        else:
            reply_to_tweet_id = target["tweet_id"]
            prompt = build_reply_prompt(
                f"Replying in an AIUNION-related conversation (tweet_id={reply_to_tweet_id})"
            )
            logger.info("Reply target set: tweet_id=%s", reply_to_tweet_id)

    if action == "deeper_thread":
        if unannounced_bounties:
            bounty = random.choice(unannounced_bounties)
        elif bounties:
            bounty = random.choice(bounties)
        else:
            logger.info("No bounties for deeper_thread — falling back to original_post")
            action = "original_post"
            bounty = None
        if bounty:
            prompt = build_deeper_thread_prompt(bounty, btc_price)
            bounty_id_to_mark = bounty["id"]
            logger.info("deeper_thread on bounty id=%s", bounty_id_to_mark)

    if action == "meta_treasury_nudge":
        prompt = build_meta_treasury_prompt(status, bounties, btc_price)
        logger.info("meta_treasury_nudge action selected")

    if action == "original_post" or prompt is None:
        if unannounced_bounties:
            bounty = random.choice(unannounced_bounties)
            prompt = build_bounty_prompt(bounty, btc_price)
            bounty_id_to_mark = bounty["id"]
            logger.info("original_post: announcing bounty id=%s", bounty_id_to_mark)
        elif proposals:
            proposal = proposals[-1]
            prompt = build_proposal_prompt(proposal, btc_price)
            logger.info("original_post: announcing proposal")
        else:
            prompt = build_treasury_prompt(status, btc_price)
            logger.info("original_post: treasury update")

    logger.info("Generating post text via Grok (action=%s)", action)
    try:
        post_text = generate_post(prompt, label_automated=True)
        logger.info("Post generated (length=%d)", len(post_text))
    except Exception as exc:
        logger.error("Grok generation failed: %s", exc)
        sys.exit(1)

    try:
        result = post_tweet(post_text, reply_to_tweet_id=reply_to_tweet_id)
        logger.info("Posted to X: tweet_id=%s", result.get("tweet_id"))
    except Exception as exc:
        logger.error("Twitter post failed: %s", exc)
        sys.exit(1)

    state["posts_today"] += 1
    if bounty_id_to_mark:
        state.setdefault("posted_bounty_ids", []).append(bounty_id_to_mark)
        state["posted_bounty_ids"] = state["posted_bounty_ids"][-500:]
    save_state(state)
    logger.info(
        "Agent run complete. action=%s posts_today=%d/%d",
        action, state["posts_today"], MAX_DAILY_POSTS
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIUNION Marketing Agent")
    parser.add_argument("--event", type=str, default=None,
                        help="Event type: new_bounty or claim_paid")
    parser.add_argument("--title", type=str, default="")
    parser.add_argument("--amount-usd", type=float, default=0.0, dest="amount_usd")
    parser.add_argument("--description", type=str, default="")
    parser.add_argument("--bounty-title", type=str, default="", dest="bounty_title")
    parser.add_argument("--claimant-name", type=str, default="", dest="claimant_name")
    parser.add_argument("--submission-url", type=str, default="", dest="submission_url")
    args = parser.parse_args()

    if args.event:
        if args.event == "new_bounty":
            payload = {
                "title": args.title,
                "amount_usd": args.amount_usd,
                "description": args.description,
            }
        elif args.event == "claim_paid":
            payload = {
                "bounty_title": args.bounty_title,
                "claimant_name": args.claimant_name,
                "amount_usd": args.amount_usd,
                "submission_url": args.submission_url,
            }
        else:
            logger.error("Unknown --event value: %s", args.event)
            sys.exit(1)
        run_event(args.event, payload)
    else:
        run()
