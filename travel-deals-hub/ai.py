"""Claude-powered extras: reading experience screenshots/text, and the weekly top-5 picks.

Credentials come from the environment (ANTHROPIC_API_KEY, or an `ant auth login` profile). Without them these
features stay off and the rest of the app works normally.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "date": {"type": "string", "description": "Date exactly as shown, or empty"},
                    "date_iso": {"type": "string", "description": "YYYY-MM-DD of the event, or empty if unknown"},
                    "venue": {"type": "string", "description": "Venue and/or city, or empty"},
                    "kind": {"type": "string", "description": "Presale, Reserved, Auction, Buy now, Raffle, or Other"},
                    "price": {"type": "string", "description": "Price or current bid as shown, or empty"},
                    "link": {"type": "string", "description": "Full URL if visible in the input, else empty"},
                },
                "required": ["name", "date", "date_iso", "venue", "kind", "price", "link"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}

PICKS_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "why": {"type": "string"},
                    "link": {"type": "string"},
                },
                "required": ["headline", "why", "link"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}


def has_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (pathlib.Path.home() / ".config" / "anthropic").exists()


def _call(content: list | str, schema: dict, effort: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("The answer was cut off; try a smaller screenshot or less text.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


def extract_events(program: str, text: str | None = None, image: tuple[bytes, str] | None = None) -> list[dict]:
    """program: 'Amex Experiences' or 'Delta SkyMiles Experiences'. image: (bytes, media_type)."""
    instructions = (
        f"The input is from the {program} page, copied by the person who uses this app. "
        "List every individual event or experience listing it shows. Skip navigation, filters, ads and footer "
        "text. Use empty strings for anything not shown; do not invent details."
    )
    content: list = []
    if image:
        data, media_type = image
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                    "data": base64.standard_b64encode(data).decode("ascii")}})
    if text:
        content.append({"type": "text", "text": f"<page_text>\n{text[:150000]}\n</page_text>"})
    content.append({"type": "text", "text": instructions})
    return _call(content, EVENT_SCHEMA, effort="low")["events"]


def weekly_summary(snap: dict, user: dict) -> str:
    lines = []
    for i in snap["items"][:120]:
        if i.get("expired") or "news" in i["tags"] and not i.get("transfer"):
            continue
        bits = [i["title"]]
        if i.get("price_usd"):
            bits.append(f"${i['price_usd']}")
        if i.get("home"):
            bits.append("departs DC area")
        if i.get("deadline"):
            bits.append(f"ends {i['deadline']}")
        lines.append(" | ".join(bits) + f" | {i['link']}")
    for u in snap.get("united_exclusives", [])[:15]:
        lines.append(f"United Exclusive: {u['title']} | {u['date']} | {u['location']} | {u['miles_text']} | {u['link']}")
    prompt = (
        "You pick the five most worthwhile travel deals of the week for one traveler who lives in Washington, DC "
        "(airports DCA, IAD, BWI), holds an Amex card and flies United and Delta. "
        f"Destinations they are watching: {', '.join(user.get('watchlist', [])) or 'none'}. "
        "Favor deals departing the DC area, deals on their watchlist, real price drops, strong transfer bonuses "
        "and experiences near DC. Skip expired deals. For each pick, write a short headline and one sentence on "
        "why it is worth their attention, and copy its link exactly from the list.\n\n"
        "<deals>\n" + "\n".join(lines) + "\n</deals>"
    )
    picks = _call(prompt, PICKS_SCHEMA, effort="medium")["picks"][:5]
    return "\n".join(f"{n}. **{p['headline']}** - {p['why']} ({p['link']})" for n, p in enumerate(picks, 1))
