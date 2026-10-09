"""Webhook notifications after a run (Discord, Slack, or any JSON endpoint)."""
from __future__ import annotations

import httpx


def summary_text(job: str, run: dict) -> str:
    return (
        f"Harvest: **{job}** run #{run['id']} {run['status']}\n"
        f"pages {run['pages']} | items {run['items']} | new {run['new_items']} | "
        f"changed {run['changed_items']} | removed {run['removed_items']} | errors {run['errors']}"
    )


async def send(webhook: str, job: str, run: dict, changes: list[dict] | None = None) -> None:
    text = summary_text(job, run)
    if changes:
        lines = []
        for c in changes[:10]:
            if c["change"] == "changed" and c.get("diff"):
                diffs = ", ".join(f"{k}: {o} -> {n}" for k, (o, n) in list(c["diff"].items())[:3])
                lines.append(f"- changed `{c['key'][:40]}`: {diffs}")
            else:
                lines.append(f"- {c['change']} `{c['key'][:40]}`")
        text += "\n" + "\n".join(lines)
        if len(changes) > 10:
            text += f"\n...and {len(changes) - 10} more"
    if "discord.com/api/webhooks" in webhook or "discordapp.com/api/webhooks" in webhook:
        payload = {"content": text[:1990]}
    elif "hooks.slack.com" in webhook:
        payload = {"text": text.replace("**", "*")}
    else:
        payload = {"job": job, "run": run, "text": text, "changes": changes or []}
    async with httpx.AsyncClient(timeout=15) as c:
        await c.post(webhook, json=payload)
