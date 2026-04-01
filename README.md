# House Chores Bot

Discord bot that assigns chores fairly among housemates and sends automated reminders.

## Setup

1. Add your Discord bot token to `.env`:
   ```
   CHORES_BOT_TOKEN=your_token_here
   ```

2. Install the systemd service:
   ```bash
   sudo cp house-chores-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable house-chores-bot
   sudo systemctl start house-chores-bot
   ```

## Managing the Bot

```bash
systemctl status house-chores-bot        # check if it's running
journalctl -u house-chores-bot -f        # live logs
sudo systemctl restart house-chores-bot  # restart manually
sudo systemctl stop house-chores-bot     # stop
```

The bot starts automatically on boot and restarts itself if it crashes.

## Slash Commands

| Command | Description |
|---------|-------------|
| `/chores` | Show this week's chore assignments |
| `/chores week:-1` | Show last week's assignments |
| `/chores week:1` | Show next week's assignments |
| `/chores ping:True` | Post and ping everyone (pins the message) |
| `/chores table:True` | Use compact table format |
| `/chores week:1 ping:True table:True` | Combine options |
| `/help` | Show help and reminder schedule |

## Automated Reminders (California Time)

| Time            | Reminder                  |
|-----------------|---------------------------|
| Sunday 6pm      | Weekly schedule posted    |
| Sunday 6pm      | Dishrack — put away dishes |
| Sunday 8pm      | Compost — take out        |
| Monday 8am      | Compost — bring back      |
| Monday 8pm      | Recycling — take out      |
| Tuesday 8am     | Recycling — bring back    |
| Thursday 6pm    | Dishrack — put away dishes |

Each reminder pings the assigned person and opens a thread for photo proof.
