#!/usr/bin/env python3
"""Discord bot for house chores reminders."""

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import discord
from http.server import HTTPServer, BaseHTTPRequestHandler

load_dotenv(Path(__file__).parent / ".env")
from discord.ext import tasks

from house_chores import load_config, get_week_ranges, assign_tasks_fairly

CONFIG_PATH = Path(__file__).parent / "discord_config.json"
SEED = 42
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", 8080))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # Suppress logs


def start_health_server():
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    server.serve_forever()


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
    today = datetime.now(ZoneInfo("America/Los_Angeles")).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    week, _ = get_week_for_date(today)
    return week["assignments"] if week else {}


def get_next_week_assignments() -> dict:
    """Get assignments for the next week."""
    today = datetime.now(ZoneInfo("America/Los_Angeles")).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    next_week = today + timedelta(days=7)
    week, _ = get_week_for_date(next_week)
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

        if message.content == "!help" or self.user in message.mentions:
            help_text = """```
HOUSE CHORES BOT
================

COMMANDS
  !chores [+-Nw] [--ping]
                     Shows week's assignments
                     -1w: previous week, +1w: next week
                     --ping: loud ping, posts in channel

AUTOMATED REMINDERS (California Time)
  Sunday 6pm     Weekly schedule posted

  Compost:
    Sunday 8pm   Take out
    Monday 8am   Bring back

  Recycling:
    Monday 8pm   Take out
    Tuesday 8am  Bring back

Each reminder pings the assigned person and creates a
"Proof of Completion" thread for photo verification.
```"""
            await message.channel.send(help_text)

        elif message.content.startswith("!chores"):
            loud_ping = "--ping" in message.content

            # Parse week offset (e.g., +1w, -2w)
            target_date = datetime.now(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)
            week_match = re.search(r'([+-]\d+)w', message.content)
            if week_match:
                weeks_offset = int(week_match.group(1))
                target_date += timedelta(weeks=weeks_offset)

            week, tasks = get_week_for_date(target_date)
            if not week:
                await message.channel.send("No schedule for that week.")
                return

            content = self.format_weekly_schedule(week, tasks, silent=not loud_ping)

            if not loud_ping:
                content += "\n_Run !chores --ping to ping everyone with notification_"
                # Reply in thread to keep channel clean
                try:
                    thread = await message.create_thread(name=f"Week {week['week_num']} Chores")
                    await thread.send(content)
                except discord.HTTPException:
                    await message.channel.send(content)
            else:
                # Unpin previous bot messages
                try:
                    pins = await message.channel.pins()
                    for pin in pins:
                        if pin.author == self.user:
                            await pin.unpin()
                except discord.HTTPException:
                    pass

                # Send and pin new message
                sent_msg = await message.channel.send(content)
                try:
                    await sent_msg.pin()
                except discord.HTTPException:
                    pass

    def format_weekly_schedule(self, week, tasks, silent=False):
        """Format the weekly schedule message."""
        start_str = week["start_date"].strftime("%b %d")
        end_str = week["end_date"].strftime("%b %d, %Y")
        partial = f" ({week['days']}d)" if week["partial"] else ""

        lines = [f"**WEEK {week['week_num']}{partial}: {start_str} - {end_str}**", ""]

        for task in tasks:
            assignee = week["assignments"].get(task["id"], "?")
            ping = format_ping(assignee, self.discord_cfg)
            lines.append(f"**{task['name']}** — {ping}")
            lines.append(f"└─ {task['schedule']}")
            lines.append(f"> {task['description']}")
            lines.append("")

        content = "\n".join(lines)
        if silent:
            content = "@silent " + content
        return content

    async def send_weekly_schedule(self):
        """Send the weekly schedule to active channels."""
        target_date = datetime.now(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)
        week, tasks = get_week_for_date(target_date)
        if not week:
            return

        content = self.format_weekly_schedule(week, tasks, silent=False)

        channels = self.discord_cfg.get("channels", {})
        active = self.discord_cfg.get("active_channels", [])

        for name in active:
            channel_id = channels.get(name)
            if channel_id:
                channel = self.get_channel(channel_id)
                if channel:
                    # Unpin previous bot messages
                    try:
                        pins = await channel.pins()
                        for pin in pins:
                            if pin.author == self.user:
                                await pin.unpin()
                    except discord.HTTPException:
                        pass

                    # Send and pin new message
                    sent_msg = await channel.send(content)
                    try:
                        await sent_msg.pin()
                    except discord.HTTPException:
                        pass

    @tasks.loop(minutes=1)
    async def reminder_loop(self):
        """Check every minute if we need to send a reminder."""
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        day_name = now.strftime("%A")
        hour = now.hour
        minute = now.minute

        # Only trigger at the start of the hour
        if minute != 0:
            return

        # Sunday 6pm - send weekly schedule
        if day_name == "Sunday" and hour == 18:
            await self.send_weekly_schedule()

        assignments = get_current_week_assignments()
        if not assignments:
            return

        channels = self.discord_cfg.get("channels", {})
        active = self.discord_cfg.get("active_channels", [])
        reminders = self.discord_cfg.get("reminders", {})

        for task_id, task_reminders in reminders.items():
            # Skip locked_doors for now - daily reminders would spam the channel
            if task_id == "locked_doors":
                continue

            for reminder in task_reminders:
                if reminder["day"] == day_name and reminder["hour"] == hour:
                    # Compost (Sunday out, Monday back): recycling_deliver person
                    # Recycling (Monday out, Tuesday back): recycling_return person
                    if task_id == "recycling_deliver":
                        if day_name == "Sunday":
                            # Compost out: next week's recycling_deliver
                            next_assignments = get_next_week_assignments()
                            assignee = next_assignments.get("recycling_deliver")
                        else:
                            # Recycling out (Monday): recycling_return person
                            assignee = assignments.get("recycling_return")
                    elif task_id == "recycling_return":
                        if day_name == "Monday":
                            # Compost back: current week's recycling_deliver
                            assignee = assignments.get("recycling_deliver")
                        else:
                            # Recycling back (Tuesday): recycling_return person
                            assignee = assignments.get("recycling_return")
                    else:
                        assignee = assignments.get(task_id)

                    if not assignee:
                        continue

                    ping = format_ping(assignee, self.discord_cfg)
                    msg = f"{ping} {reminder['message']}"
                    proof_msg = reminder.get("proof", "Send image proof upon completion")
                    for name in active:
                        channel_id = channels.get(name)
                        if channel_id:
                            channel = self.get_channel(channel_id)
                            if channel:
                                sent_msg = await channel.send(msg)
                                thread = await sent_msg.create_thread(name="Proof of Completion")
                                await thread.send(proof_msg)

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

    # Start health check server in background
    health_thread = Thread(target=start_health_server, daemon=True)
    health_thread.start()
    print(f"Health server running on port {HEALTH_PORT}")

    bot = ChoresBot()
    bot.run(token)
    return 0


if __name__ == "__main__":
    exit(main())
