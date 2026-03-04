# Email → Motion / Calendar

Automatically converts forwarded emails into Motion tasks or Outlook calendar events using Claude AI.

Forward an email to a Slack channel → Claude extracts the details → a Motion task or calendar invite is created within seconds.

---

## How it works

Two independent pipelines share the same codebase:

**Tasks pipeline** (`#email-to-motion`)
Forward any email that requires action. Claude reads the email, extracts one or more tasks (title, priority, duration, due date), and creates them in Motion with auto-scheduling enabled. A ✅ confirmation is posted back as a Slack thread reply.

**Calendar pipeline** (`#email-to-calendar`)
Forward any email containing event information. Claude extracts the event details (title, time, location, description) and emails you a `.ics` calendar invite that Outlook recognises as an Accept/Decline invite.

Both channels use Slack's built-in email integration — each channel has a unique inbound email address you forward to.

---

## Day-to-day use

### Creating a task
1. Receive an email that requires action.
2. Forward it to your *"Forward to Motion"* contact (the `#email-to-motion` channel address).
3. Within the next polling interval (default 30 min, or immediately if run manually), the task appears in Motion auto-scheduled, and a ✅ reply appears in Slack showing the task name, priority, duration, and due date.

For emails with clearly separate actions (e.g. "make a rubric" and "grade the exams"), Claude will create multiple tasks automatically.

### Creating a calendar event
1. Receive an email about a meeting, seminar, defence, or any fixed-time event.
2. Forward it to your *"Forward to Calendar"* contact (the `#email-to-calendar` address).
3. A `.ics` invite lands in your inbox. Open it in Outlook to add the event to your calendar.

### Running manually
```bash
cd ~/path/to/EmailToMotion
python -m email_to_motion
```

Run only one pipeline:
```bash
python -m email_to_motion --tasks-only
python -m email_to_motion --calendar-only
```

Run continuously (checks every 30 minutes):
```bash
python -m email_to_motion --loop
python -m email_to_motion --loop --interval 15
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in each value — see the section below for where to find each one.

### 3. Get your Motion Workspace ID

```bash
python -m email_to_motion --workspaces
```

Copy the ID shown and paste it into `.env` next to `MOTION_WORKSPACE_ID=`.

### 4. Set up the Slack channels

Create both channels in Slack and invite your bot to each:
```
/invite @YourBotName
```

Get each channel's inbound email address via **⚙️ gear → Integrations → Send emails to this channel**, then save them as contacts in your email client:
- `#email-to-motion` → save as *"Forward to Motion"*
- `#email-to-calendar` → save as *"Forward to Calendar"*

### 5. Test end-to-end

```bash
python -m email_to_motion
```

Forward a test email to each channel and confirm a task appears in Motion and a calendar invite arrives in your inbox.

---

## Environment variables

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | Bot User OAuth Token from api.slack.com/apps → OAuth & Permissions |
| `SLACK_MOTION_CHANNEL` | Slack channel name for tasks (default: `email-to-motion`) |
| `SLACK_CALENDAR_CHANNEL` | Slack channel name for calendar events (default: `email-to-calendar`) |
| `MOTION_API_KEY` | From usemotion.com → Settings → API |
| `MOTION_WORKSPACE_ID` | Run `python -m email_to_motion --workspaces` to find this |
| `ANTHROPIC_API_KEY` | From console.anthropic.com/settings/keys |
| `SMTP_USER` | Gmail address used to send calendar invites |
| `SMTP_PASSWORD` | Gmail App Password (myaccount.google.com → Security → App passwords) |
| `CALENDAR_EMAIL` | The address where calendar invites are delivered |

---

## Running automatically in the background (macOS)

Edit `com.degroot.email-to-motion.plist` and fill in your paths and environment variables, then:

```bash
cp com.degroot.email-to-motion.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.degroot.email-to-motion.plist
```

View logs:
```bash
tail -f /tmp/email_to_motion.log
```

Stop the background process:
```bash
launchctl unload ~/Library/LaunchAgents/com.degroot.email-to-motion.plist
```

---

## Linux server deployment

### 1. Clone the repository and install

```bash
git clone https://github.com/YOUR_USERNAME/EmailToMotion.git
cd EmailToMotion
pip install -e .
```

The `-e` flag installs in **editable mode** — the `email-to-motion` command runs directly from the git checkout. Running `git pull` later will update the code immediately without any reinstall.

### 2. Create your .env file

```bash
cp .env.example .env
nano .env   # fill in your credentials
```

### 3. Configure the systemd service

Edit `systemd/email-to-motion.service` and replace the two placeholders:
- `YOUR_USERNAME` → your Linux username (e.g. `chris`)
- `/path/to/EmailToMotion` → the full path to the cloned repo (both occurrences)

Then install and start the service:

```bash
sudo cp systemd/email-to-motion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable email-to-motion    # start automatically on boot
sudo systemctl start email-to-motion     # start now
```

### 4. Check it is running

```bash
systemctl status email-to-motion
journalctl -u email-to-motion -f        # follow live logs
```

### Updating the code

Pull the latest changes and restart the service with the included script:

```bash
./scripts/update.sh
```

This runs `git pull` and then `sudo systemctl restart email-to-motion`. The restart is necessary because the process is running in `--loop` mode and holds the old code in memory until restarted.

To allow the update script to restart the service without a password prompt, add this line to your sudoers file (`sudo visudo`):

```
chris ALL=(ALL) NOPASSWD: /bin/systemctl restart email-to-motion
```

---

## Project structure

```
EmailToMotion/
├── email_to_motion/
│   ├── config.py          # Environment variables and shared client initialisation
│   ├── slack_helpers.py   # Channel monitoring, message extraction, emoji marking
│   ├── tasks.py           # Task extraction (Claude + Motion API) and pipeline
│   ├── events.py          # Event extraction (Claude + ICS + SMTP) and pipeline
│   ├── cli.py             # Argument parsing and main loop
│   └── __main__.py        # Enables `python -m email_to_motion`
├── systemd/
│   └── email-to-motion.service   # systemd unit file for Linux server deployment
├── scripts/
│   └── update.sh          # Pull latest code and restart the service
├── pyproject.toml          # Package definition — enables `pip install -e .`
├── requirements.txt
├── .env.example
├── .env                   # Your credentials — never commit this
```
