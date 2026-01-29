#!/usr/bin/env python3
"""Discord bot for house chores reminders."""

import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import discord

load_dotenv(Path(__file__).parent / ".env")
from discord.ext import tasks

from house_chores import load_config, get_week_ranges, assign_tasks_fairly

CONFIG_PATH = Path(__file__).parent / "discord_config.json"
SEED = 42


def load_discord_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_schedule():
    """Get full schedule."""
    config = load_config()

    start = datetime.strptime(config["start_date"], "%Y-%m-%d")
    end = datetime.strptime(config["end_date"], "%Y-%m-%d")

    weeks = get_week_ranges(start, end)
    schedule, _ = assign_tasks_fairly(config["tasks"], weeks, config["assignees"], SEED)

    return schedule, config["tasks"]


def get_week_for_date(target_date: datetime) -> dict | None:
    """Get assignments for the week containing target_date."""
    schedule, tasks = get_schedule()

    for week in schedule:
        if week["start_date"] <= target_date <= week["end_date"]:
            return week, tasks

    return None, None


def get_current_week_assignments() -> dict:
    """Get assignments for the current week."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    week, _ = get_week_for_date(today)
    return week["assignments"] if week else {}


def format_ping(assignee: str, discord_cfg: dict) -> str:
    """Convert assignee name to Discord pings."""
    user_ids = discord_cfg["user_ids"].get(assignee, [])
    if user_ids:
        return " ".join(f"<@{uid}>" for uid in user_ids)
    return assignee


class ChoresBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.discord_cfg = load_discord_config()

    async def setup_hook(self):
        self.reminder_loop.start()

    async def on_ready(self):
        print(f"Bot ready: {self.user}")

    async def on_message(self, message):
        if message.author == self.user:
            return

        if message.content == "!test":
            assignments = get_current_week_assignments()
            if not assignments:
                await message.channel.send("No assignments for current week.")
                return

            lines = ["**This week's recycling assignments:**"]
            for task_id in ["recycling_deliver", "recycling_return"]:
                assignee = assignments.get(task_id, "?")
                ping = format_ping(assignee, self.discord_cfg)
                task_name = task_id.replace("_", " ").title()
                lines.append(f"- {task_name}: {ping}")
            await message.channel.send("\n".join(lines))

        elif message.content.startswith("!week"):
            do_ping = "-ping" in message.content

            target_date = datetime.now()
            week, tasks = get_week_for_date(target_date)
            if not week:
                await message.channel.send("No schedule for current week.")
                return

            # Format week display
            start_str = week["start_date"].strftime("%b %d")
            end_str = week["end_date"].strftime("%b %d, %Y")
            partial = f" ({week['days']}d)" if week["partial"] else ""

            lines = [f"**Week {week['week_num']}{partial}: {start_str} - {end_str}**", ""]

            for task in tasks:
                assignee = week["assignments"].get(task["id"], "?")
                if do_ping:
                    name = format_ping(assignee, self.discord_cfg)
                else:
                    name = assignee
                lines.append(f"**{task['name']}**")
                lines.append(f"_{task['description']}_")
                lines.append(f"📅 {task['schedule']}")
                lines.append(f"👤 {name}")
                lines.append("")

            if not do_ping:
                lines.append("_Run `!week -ping` to ping everyone_")

            await message.channel.send("\n".join(lines))

    @tasks.loop(minutes=1)
    async def reminder_loop(self):
        """Check every minute if we need to send a reminder."""
        now = datetime.now()
        day_name = now.strftime("%A")
        hour = now.hour
        minute = now.minute

        # Only trigger at the start of the hour
        if minute != 0:
            return

        assignments = get_current_week_assignments()
        if not assignments:
            return

        channel = self.get_channel(self.discord_cfg["channel_id"])
        if not channel:
            return

        reminders = self.discord_cfg.get("reminders", {})

        for task_id, task_reminders in reminders.items():
            assignee = assignments.get(task_id)
            if not assignee:
                continue

            for reminder in task_reminders:
                if reminder["day"] == day_name and reminder["hour"] == hour:
                    ping = format_ping(assignee, self.discord_cfg)
                    message = f"{ping} {reminder['message']}"
                    await channel.send(message)

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.wait_until_ready()


def main():
    cfg = load_discord_config()

    # Support both direct token and env var
    token = cfg.get("bot_token")
    if not token:
        token_env = cfg.get("bot_token_env", "CHORES_BOT_TOKEN")
        token = os.environ.get(token_env)
        if not token:
            print(f"Error: Set bot_token in discord_config.json or {token_env} env var")
            return 1

    bot = ChoresBot()
    bot.run(token)
    return 0


if __name__ == "__main__":
    exit(main())
