"""
tasks.py — Task extraction pipeline.

Responsibilities:
  1. Send email text to Claude and get back structured task JSON.
  2. Create the task(s) in Motion via the REST API.
  3. Post a confirmation message back to Slack.
  4. Orchestrate the above for every unprocessed message in the tasks channel.
"""

import json
import requests
from datetime import date, timedelta
from . import config
from .slack_helpers import get_channel_id, get_unprocessed_messages, mark_processed, extract_email_text
from .utils import parse_claude_json

# ── Channel ID cache ──────────────────────────────────────────────────────────
# Resolved once on first use to avoid a full paginated Slack API call every poll.
_channel_id: str = ""


def _get_channel_id() -> str:
    global _channel_id
    if not _channel_id:
        _channel_id = get_channel_id(config.SLACK_MOTION_CHANNEL_NAME)
    return _channel_id

# ── Claude prompts ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a task extraction assistant. Given an email (which may be forwarded "
    "and include headers), identify all clearly distinct actions the recipient needs "
    "to take and return ONLY a JSON array of task objects — no markdown, no explanation. "
    "Default to one task, but split into multiple when actions are independent in nature, "
    "have different deadlines, or would naturally be done in separate sessions."
)

USER_PROMPT_TEMPLATE = """\
Analyze this email and extract task metadata.

EMAIL:
{email_text}

Return a JSON array of one or more task objects. Each task has exactly these keys \
(no extras, no markdown fences around the array):
[
  {{
    "name": "<action-oriented title, max 80 chars, start with a verb>",
    "description": "<GitHub Markdown — see description rules below>",
    "priority": "<ASAP|HIGH|MEDIUM|LOW>",
    "duration": <integer minutes>,
    "dueDate": "<YYYY-MM-DD or null>",
    "deadlineType": "<HARD|SOFT|NONE>"
  }}
]

Task-splitting rules:
  - Default to ONE task. Only create multiple tasks when the email contains actions that
    are clearly independent — different in nature, could be done on separate days, or would
    logically be assigned to different sessions.
  - Good reasons to split: (i) different deliverables (e.g., make a rubric vs. grade exams
    vs. release grades), (ii) actions with different deadlines, (iii) one task is a
    prerequisite but could be completed well before the next.
  - Do NOT split: sequential steps of the same activity, minor subtasks that belong under
    one heading, or actions that would naturally be done in one sitting.

Description rules:
 - Format using GitHub Markdown (bold, bullet lists, headers, inline code, links, etc.).
 - The user must not need to refer back to the original email to understand what to do —
   include all relevant details, context, links, and instructions.
 - Start with a 1–2 sentence summary of what needs to be done and why.
 - Use a bullet list for subtasks or steps. Include every step, even obvious ones.
 - Preserve any important links from the email as Markdown hyperlinks.
 - Include relevant contact details (name, email) if the task involves responding to someone.
 - End with a "**Source**" line noting the sender and original subject.

Priority rules:
  ASAP   — urgent right now: someone is actively waiting, the task is already overdue,
            or it must be done today
  HIGH   — genuinely time-sensitive with serious consequences if missed: hard deadlines,
            commitments to others that cannot slip, or preparation for a fixed external
            event (e.g., an exam, a scheduled class, a grant submission).
            Do NOT use HIGH simply because a task has a deadline or feels important —
            most tasks with deadlines are MEDIUM.
  MEDIUM — default for normal work: has a deadline, matters, but a short delay would not
            cause serious harm
  LOW    — low-stakes or open-ended: no real deadline, no significant consequences if deferred

Duration rules:
  Estimate realistically in minutes based on the actual work described. Think about how long
  the task would actually take a competent person to complete, accounting for complexity,
  number of steps, and any reading or research involved.
  Round to the nearest 15 minutes (e.g. 15, 30, 45, 60, 90, 120, …).
"""


# ── Claude analysis ───────────────────────────────────────────────────────────

def analyze_with_claude(email_text: str) -> list[dict]:
    response = config.claude.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": USER_PROMPT_TEMPLATE.format(email_text=email_text)}],
    )
    result = parse_claude_json(response.content[0].text)
    return result if isinstance(result, list) else [result]


# ── Motion API ────────────────────────────────────────────────────────────────

MOTION_BASE = "https://api.usemotion.com/v1"


def _motion_headers() -> dict:
    return {
        "X-API-Key":    config.MOTION_API_KEY,
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }


def get_workspaces() -> list:
    r = requests.get(f"{MOTION_BASE}/workspaces", headers=_motion_headers(), timeout=10)
    r.raise_for_status()
    return r.json().get("workspaces", [])


def create_motion_task(task: dict) -> dict:
    due = task.get("dueDate") or (date.today() + timedelta(weeks=2)).isoformat()
    payload = {
        "name":        task["name"],
        "workspaceId": config.MOTION_WORKSPACE_ID,
        "description": task.get("description", ""),
        "priority":    task.get("priority", "MEDIUM"),
        "duration":    task.get("duration", 30),
        "assigneeId":  config.MOTION_ASSIGNEE_ID,
        "dueDate":     due,
        "autoScheduled": {
            "deadlineType": task.get("deadlineType", "SOFT"),
            "schedule":     "Work Hours",
        },
    }
    r = requests.post(f"{MOTION_BASE}/tasks", headers=_motion_headers(), json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


# ── Slack confirmation ────────────────────────────────────────────────────────

def _post_confirmation(channel_id: str, ts: str, tasks: list[dict]):
    lines = [f"✅ *{len(tasks)} Motion task{'s' if len(tasks) > 1 else ''} created*"]
    for task in tasks:
        due = task.get("dueDate") or "flexible"
        lines.append(
            f"• *{task['name']}* — "
            f"{task['priority']} · {task['duration']} min · due {due}"
        )
    config.slack.chat_postMessage(channel=channel_id, thread_ts=ts, text="\n".join(lines))


# ── Pipeline ──────────────────────────────────────────────────────────────────

def process_channel() -> int:
    """Process all unhandled emails in the tasks Slack channel. Returns number of tasks created."""
    channel_id = _get_channel_id()
    messages   = get_unprocessed_messages(channel_id)

    if not messages:
        print("  No unprocessed messages.")
        return 0

    print(f"  Found {len(messages)} unprocessed message(s).")
    created = 0

    for msg in messages:
        text = extract_email_text(msg)

        if len(text) < 20:
            print(f"  Skipping short message: {text[:40]!r}")
            continue

        print(f"\n  ▶ Analyzing: {text[:80]}…")
        try:
            tasks = analyze_with_claude(text)
            print(f"    Claude identified {len(tasks)} task(s).")
            created_tasks = []
            for task in tasks:
                print(
                    f"    • {task['name']} "
                    f"[{task['priority']} · {task['duration']} min · "
                    f"due {task.get('dueDate') or 'flexible'}]"
                )
                create_motion_task(task)
                created_tasks.append(task)

            mark_processed(channel_id, msg["ts"])
            _post_confirmation(channel_id, msg["ts"], created_tasks)
            print(f"    ✅ {len(created_tasks)} Motion task(s) created.")
            created += len(created_tasks)

        except json.JSONDecodeError as e:
            print(f"    ✗ Claude returned invalid JSON: {e}")
        except requests.HTTPError as e:
            print(f"    ✗ Motion API error: {e.response.status_code} {e.response.text}")
        except Exception as e:
            print(f"    ✗ Unexpected error: {e}")

    print(f"\n  Done. Created {created} Motion task(s) from {len(messages)} email(s).")
    return created
