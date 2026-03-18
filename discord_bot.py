#!/usr/bin/env python3
"""Discord bot for house chores reminders."""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import discord
from http.server import HTTPServer, BaseHTTPRequestHandler

load_dotenv(Path(__file__).parent / ".env")

from house_chores import load_config, get_week_ranges, assign_tasks_fairly

CONFIG_PATH = Path(__file__).parent / "discord_config.json"
SEED = 42
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", 8080))
TIMEZONE = ZoneInfo("America/Los_Angeles")
FOLLOWUP_MARKER = "this is a reminder"


def today() -> datetime:
    """Get today's date at midnight in local timezone (naive datetime)."""
    return datetime.now(TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


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
    with open(CONFIG_PATH, encoding="utf-8") as f:
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
    week, _ = get_week_for_date(today())
    return week["assignments"] if week else {}


def get_task_name(task_id: str) -> str:
    """Get task name from task ID."""
    _, tasks = get_schedule()
    for task in tasks:
        if task["id"] == task_id:
            return task["name"]
    return task_id  # Fallback to ID if not found


def get_next_week_assignments() -> dict:
    """Get assignments for the next week."""
    next_week = today() + timedelta(days=7)
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

    async def send_and_pin(self, channel, content):
        """Unpin previous bot messages, send new message, and pin it."""
        try:
            pins = await channel.pins()
            for pin in pins:
                if pin.author == self.user:
                    await pin.unpin()
        except discord.HTTPException:
            pass

        sent_msg = await channel.send(content)
        try:
            await sent_msg.pin()
        except discord.HTTPException:
            pass
        return sent_msg

    def iter_active_channels(self):
        """Yield active channel objects."""
        channels = self.discord_cfg.get("channels", {})
        for name in self.discord_cfg.get("active_channels", []):
            channel_id = channels.get(name)
            if channel_id:
                channel = self.get_channel(channel_id)
                if channel:
                    yield channel

    async def was_recently_sent(self, channel, text_fragment: str, minutes: int = 60) -> bool:
        """Check if bot already sent a message containing text_fragment within the last N minutes."""
        cutoff = discord.utils.utcnow() - timedelta(minutes=minutes)
        try:
            async for message in channel.history(limit=50, after=cutoff):
                if message.author == self.user and text_fragment in message.content:
                    return True
        except discord.HTTPException:
            pass
        return False

    async def followup_already_sent(self, thread) -> bool:
        """Check if we already sent a follow-up in this thread."""
        async for message in thread.history(limit=20):
            if message.author == self.user and FOLLOWUP_MARKER in message.content:
                return True
        return False

    async def assigned_user_has_replied(self, thread, parent_message) -> bool:
        """Check if the assigned user(s) have replied in the thread."""
        assigned_user_ids = set(re.findall(r'<@(\d+)>', parent_message.content))
        if not assigned_user_ids:
            return True

        async for message in thread.history(limit=50):
            if str(message.author.id) in assigned_user_ids:
                return True
        return False

    async def send_followup(self, thread, parent_message):
        """Send a follow-up reminder in the thread."""
        pings = re.findall(r'<@\d+>', parent_message.content)
        ping_str = " ".join(pings) if pings else ""
        await thread.send(f"{ping_str}, {FOLLOWUP_MARKER} to send image proof of completion")

    async def check_threads_for_followup(self):
        """Check proof threads and send follow-ups if needed."""
        now = discord.utils.utcnow()
        min_age = timedelta(hours=2)
        max_age = timedelta(hours=3)  # 1-hour window prevents repeat checks

        for channel in self.iter_active_channels():
            # Check both active and archived threads
            all_threads = list(channel.threads)
            async for archived in channel.archived_threads(limit=20):
                all_threads.append(archived)

            for thread in all_threads:
                thread_age = now - thread.created_at
                if not (min_age <= thread_age <= max_age):
                    continue

                # Fetch parent message to check if it's a bot reminder thread
                try:
                    parent_message = await thread.parent.fetch_message(thread.id)
                except discord.NotFound:
                    continue

                # Only process threads created from bot messages
                if parent_message.author != self.user:
                    continue

                if await self.followup_already_sent(thread):
                    continue

                if await self.assigned_user_has_replied(thread, parent_message):
                    continue

                await self.send_followup(thread, parent_message)

    async def setup_hook(self):
        asyncio.create_task(self._watchdog_reminder_loop())

    async def _watchdog_reminder_loop(self):
        """Restart reminder_loop if it ever exits unexpectedly."""
        while not self.is_closed():
            try:
                await self.reminder_loop()
            except asyncio.CancelledError:
                break  # Bot is shutting down — exit cleanly
            except Exception as e:
                print(f"[watchdog] reminder_loop crashed: {e}, restarting in 5s")
                await asyncio.sleep(5)

    async def on_ready(self):
        print(f"Bot ready: {self.user}")

    async def on_message(self, message):
        if message.author == self.user:
            return

        if message.content == "!help" or self.user in message.mentions:
            print(f"[{datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}] {message.author} in #{message.channel}: {message.content!r}")
            help_text = """```
HOUSE CHORES BOT
================

COMMANDS
  !chores [+-Nw] [--ping] [--table]
                     Shows week's assignments
                     -1w: previous week, +1w: next week
                     --ping: loud ping, posts in channel
                     --table: compact table format

AUTOMATED REMINDERS (California Time)
  Sunday 6pm     Weekly schedule posted

  Dishrack:
    Sunday 6pm   Put away dishes
    Thursday 6pm Put away dishes

  Compost:
    Sunday 8pm   Take out
    Monday 8am   Bring back

  Recycling:
    Monday 8pm   Take out
    Tuesday 8am  Bring back

Each reminder pings the assigned person and creates a
thread (named after the task) for photo verification.
```"""
            await message.channel.send(help_text)

        elif message.content.startswith("!chores"):
            print(f"[{datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}] {message.author} in #{message.channel}: {message.content!r}")
            loud_ping = "--ping" in message.content
            table_format = "--table" in message.content

            # Parse week offset (e.g., +1w, -2w)
            target_date = today()
            week_match = re.search(r'([+-]\d+)w', message.content)
            if week_match:
                weeks_offset = int(week_match.group(1))
                target_date += timedelta(weeks=weeks_offset)

            week, tasks = get_week_for_date(target_date)
            if not week:
                await message.channel.send("No schedule for that week.")
                return

            content = self.format_weekly_schedule(week, tasks, table=table_format, ping=loud_ping)

            if not loud_ping:
                content += "\n_Run !chores --ping to ping everyone with notification_"
                # Reply in thread to keep channel clean
                try:
                    thread = await message.create_thread(name=f"Week {week['week_num']} Chores")
                    await thread.send(content, silent=True)
                except discord.HTTPException:
                    await message.channel.send(content, silent=True)
            else:
                await self.send_and_pin(message.channel, content)

    def format_weekly_schedule(self, week, tasks, table=False, ping=False):
        """Format the weekly schedule message."""
        start_str = week["start_date"].strftime("%b %d")
        end_str = week["end_date"].strftime("%b %d, %Y")
        partial = f" ({week['days']}d)" if week["partial"] else ""
        header = f"WEEK {week['week_num']}{partial}: {start_str} - {end_str}"

        if table:
            # Calculate column widths
            rows = []
            assignees = set()
            for task in tasks:
                assignee = week["assignments"].get(task["id"], "?")
                assignees.add(assignee)
                rows.append((assignee, task["name"], task["schedule"]))

            col1_w = max(len(r[0]) for r in rows)
            col2_w = max(len(r[1]) for r in rows)
            col3_w = max(len(r[2]) for r in rows)

            lines = [header, "-" * len(header), ""]
            lines.append(f"{'Person':<{col1_w}}  {'Task':<{col2_w}}  {'Schedule':<{col3_w}}")
            lines.append(f"{'-'*col1_w}  {'-'*col2_w}  {'-'*col3_w}")
            for assignee, name, schedule in rows:
                lines.append(f"{assignee:<{col1_w}}  {name:<{col2_w}}  {schedule:<{col3_w}}")

            content = "```\n" + "\n".join(lines) + "\n```"

            if ping:
                pings = [format_ping(a, self.discord_cfg) for a in assignees]
                content += f"\ncc: {' '.join(pings)}"
        else:
            lines = [f"**{header}**", ""]

            for task in tasks:
                assignee = week["assignments"].get(task["id"], "?")
                mention = format_ping(assignee, self.discord_cfg)
                lines.append(f"**{task['name']}** — {mention}")
                lines.append(f"└─ {task['schedule']}")
                lines.append(f"> {task['description']}")
                lines.append("")

            content = "\n".join(lines)

        return content

    async def send_weekly_schedule(self):
        """Send the weekly schedule to active channels."""
        # Get next week's schedule (since this runs Sunday evening, we want the upcoming Mon-Sun)
        target_date = today() + timedelta(days=1)  # Move to Monday = next week
        week, tasks = get_week_for_date(target_date)
        if not week:
            return

        content = self.format_weekly_schedule(week, tasks)

        # Use week number as unique identifier for dedup
        week_marker = f"WEEK {week['week_num']}"

        for channel in self.iter_active_channels():
            # Check if already sent this week's schedule recently
            if await self.was_recently_sent(channel, week_marker):
                continue

            await self.send_and_pin(channel, content)

    async def reminder_loop(self):
        """Check every minute if we need to send a reminder.

        Sleeps until the next wall-clock minute boundary on each iteration so
        drift never accumulates — contrast with asyncio.sleep(60) which drifts
        by however long the loop body takes, compounding over days/weeks.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            now = datetime.now(TIMEZONE)
            seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
            await asyncio.sleep(seconds_until_next_minute)

            try:
                now = datetime.now(TIMEZONE)
                day_name = now.strftime("%A")
                hour = now.hour

                # Sunday 6pm - send weekly schedule
                if day_name == "Sunday" and hour == 18:
                    await self.send_weekly_schedule()

                assignments = get_current_week_assignments()
                if assignments:
                    next_assignments = get_next_week_assignments()
                    reminders = self.discord_cfg.get("reminders", {})

                    # Mapping: (task_id, day) -> (assignment_source, assignment_key)
                    # Compost (Sunday out, Monday back): recycling_deliver person
                    # Recycling (Monday out, Tuesday back): recycling_return person
                    ASSIGNEE_OVERRIDES = {
                        ("recycling_deliver", "Sunday"): ("next", "recycling_deliver"),   # Compost out
                        ("recycling_deliver", "Monday"): ("current", "recycling_return"), # Recycling out
                        ("recycling_return", "Monday"): ("current", "recycling_deliver"), # Compost back
                        ("recycling_return", "Tuesday"): ("current", "recycling_return"), # Recycling back
                    }

                    for task_id, task_reminders in reminders.items():
                        # Skip locked_doors for now - daily reminders would spam the channel
                        if task_id == "locked_doors":
                            continue

                        for reminder in task_reminders:
                            if reminder["day"] != day_name or reminder["hour"] != hour:
                                continue

                            key = (task_id, day_name)
                            if key in ASSIGNEE_OVERRIDES:
                                source, assign_key = ASSIGNEE_OVERRIDES[key]
                                pool = next_assignments if source == "next" else assignments
                                assignee = pool.get(assign_key)
                            else:
                                assignee = assignments.get(task_id)

                            if not assignee:
                                continue

                            ping = format_ping(assignee, self.discord_cfg)
                            msg = f"{ping} {reminder['message']}"
                            proof_msg = reminder.get("proof", "Send image proof upon completion")

                            for channel in self.iter_active_channels():
                                # Check if already sent this reminder recently
                                if await self.was_recently_sent(channel, reminder["message"]):
                                    continue
                                sent_msg = await channel.send(msg)
                                thread_name = get_task_name(task_id)
                                thread = await sent_msg.create_thread(name=thread_name)
                                await thread.send(proof_msg)

                # Check for threads needing follow-up reminders
                await self.check_threads_for_followup()
            except Exception as e:
                print(f"[reminder_loop] error (will retry next minute): {e}")


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

    backoff = 5
    while True:
        try:
            bot = ChoresBot()
            bot.run(token)
            return 0  # Clean shutdown (e.g. Ctrl-C)
        except discord.LoginFailure:
            print("Login failed — invalid token, not retrying")
            return 1
        except Exception as e:
            print(f"Bot crashed: {e!r}, restarting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)  # cap at 5 minutes


if __name__ == "__main__":
    exit(main())
