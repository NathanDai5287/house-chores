#!/usr/bin/env python3
"""House Chores Scheduler - Fair weekly task assignment"""

import argparse
import json
import random
from datetime import datetime, timedelta
from pathlib import Path


def load_config(config_path: str = None) -> dict:
    """Load configuration from JSON file."""
    if config_path is None:
        config_path = Path(__file__).parent / "config.json"
    with open(config_path) as f:
        return json.load(f)


def get_week_ranges(start_date: datetime, end_date: datetime) -> list[dict]:
    """Generate list of week info dicts.

    Weeks run Monday -> Sunday. Partial weeks show number of days.
    """
    weeks = []

    # Find the Monday on or before start_date
    days_since_monday = start_date.weekday()  # Monday = 0
    week_monday = start_date - timedelta(days=days_since_monday)

    week_num = 1
    while week_monday <= end_date:
        week_sunday = week_monday + timedelta(days=6)

        # Clamp to actual start/end dates
        actual_start = max(week_monday, start_date)
        actual_end = min(week_sunday, end_date)

        # Calculate days in this week
        days = (actual_end - actual_start).days + 1

        weeks.append({
            "week_num": week_num,
            "start_date": actual_start,
            "end_date": actual_end,
            "days": days,
            "partial": days < 7
        })

        week_monday = week_monday + timedelta(days=7)
        week_num += 1

    return weeks


def assign_tasks_fairly(tasks: list, weeks: list, assignees: list, seed: int) -> tuple[list, dict]:
    """Assign tasks to weeks ensuring fairness across all assignees.

    Uses round-robin rotation with task offsets:
    - Shuffle assignees once at start (deterministic with seed)
    - Each task starts at a different offset so no one gets multiple tasks per week
    - Each week, the rotation advances by 1

    This ensures:
    - No person is assigned multiple tasks in the same week (if #tasks <= #assignees)
    - Everyone rotates through all tasks equally
    - Fair distribution over time
    """
    random.seed(seed)

    n_assignees = len(assignees)
    n_tasks = len(tasks)

    if n_tasks > n_assignees:
        print(f"Warning: More tasks ({n_tasks}) than assignees ({n_assignees}). "
              "Some people will have multiple tasks per week.")

    # Shuffle assignees once to randomize starting positions
    rotation = assignees.copy()
    random.shuffle(rotation)

    # Track assignment counts per assignee per task for fairness
    assignment_counts = {}
    for task in tasks:
        assignment_counts[task["id"]] = {a: 0 for a in assignees}

    # Generate schedule
    schedule = []
    for week_idx, week in enumerate(weeks):
        week_assignments = {
            "week_num": week["week_num"],
            "start_date": week["start_date"],
            "end_date": week["end_date"],
            "days": week["days"],
            "partial": week["partial"],
            "assignments": {}
        }

        for task_idx, task in enumerate(tasks):
            task_id = task["id"]
            # Each task gets a different offset, rotation advances each week
            assignee_idx = (week_idx + task_idx) % n_assignees
            assignee = rotation[assignee_idx]

            week_assignments["assignments"][task_id] = assignee
            assignment_counts[task_id][assignee] += 1

        schedule.append(week_assignments)

    return schedule, assignment_counts


def format_date_range(start: datetime, end: datetime) -> str:
    """Format date range for display."""
    if start.month == end.month:
        return f"{start.strftime('%b %d')} - {end.strftime('%d')}"
    else:
        return f"{start.strftime('%b %d')} - {end.strftime('%b %d')}"


def render_table(schedule: list, tasks: list) -> None:
    """Render the schedule as a formatted table."""
    # Build table data
    headers = ["Week", "Dates"] + [t["name"] for t in tasks]
    rows = []

    for week in schedule:
        date_range = format_date_range(week["start_date"], week["end_date"])
        # Show partial weeks with day count
        if week["partial"]:
            week_label = f"Week {week['week_num']} ({week['days']}d)"
        else:
            week_label = f"Week {week['week_num']}"
        row = [week_label, date_range]
        for task in tasks:
            row.append(week["assignments"].get(task["id"], ""))
        rows.append(row)

    # Calculate column widths
    col_widths = [max(len(str(row[i])) for row in [headers] + rows) + 2
                  for i in range(len(headers))]

    # Print header
    header_line = "".join(f"{h:<{col_widths[i]}}" for i, h in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))

    # Print rows
    for row in rows:
        print("".join(f"{str(cell):<{col_widths[i]}}" for i, cell in enumerate(row)))


def print_fairness_summary(assignment_counts: dict, tasks: list) -> None:
    """Print summary of assignment distribution for fairness check."""
    print("\n" + "=" * 60)
    print("FAIRNESS SUMMARY")
    print("=" * 60)

    for task in tasks:
        task_id = task["id"]
        counts = assignment_counts[task_id]
        print(f"\n{task['name']}:")
        for assignee, count in sorted(counts.items(), key=lambda x: -x[1]):
            if count > 0:
                print(f"  {assignee}: {count} weeks")


def main():
    parser = argparse.ArgumentParser(
        description="Generate fair house chores schedule",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                           # Uses dates from config.json
  %(prog)s --seed 123                # Custom seed
  %(prog)s --start 2025-01-27 --end 2025-05-15  # Override dates
  %(prog)s --fairness                # Show fairness summary
        """
    )
    parser.add_argument("--start", type=str, default=None,
                        help="Start date (YYYY-MM-DD), overrides config")
    parser.add_argument("--end", type=str, default=None,
                        help="End date (YYYY-MM-DD), overrides config")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for shuffling (default: 42)")
    parser.add_argument("--config", type=str, default=None,
                        help="Path to config JSON file")
    parser.add_argument("--fairness", action="store_true",
                        help="Show fairness summary")

    args = parser.parse_args()

    # Load config
    config = load_config(args.config)
    tasks = config["tasks"]
    assignees = config["assignees"]

    # Get dates from args or config
    start_str = args.start or config.get("start_date")
    end_str = args.end or config.get("end_date")

    if not start_str or not end_str:
        print("Error: start_date and end_date must be set in config or via --start/--end")
        return 1

    # Parse dates
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d")
        end = datetime.strptime(end_str, "%Y-%m-%d")
    except ValueError as e:
        print(f"Error parsing dates: {e}")
        print("Use format YYYY-MM-DD")
        return 1

    if end < start:
        print("Error: end_date must be after start_date")
        return 1

    # Generate weeks
    weeks = get_week_ranges(start, end)

    print(f"House Chores Schedule: {start_str} to {end_str}")
    print(f"Seed: {args.seed}")
    print(f"Total weeks: {len(weeks)}")
    print()

    # Generate schedule
    schedule, assignment_counts = assign_tasks_fairly(tasks, weeks, assignees, args.seed)

    # Render table
    render_table(schedule, tasks)

    # Show fairness summary if requested
    if args.fairness:
        print_fairness_summary(assignment_counts, tasks)

    return 0


if __name__ == "__main__":
    exit(main())
