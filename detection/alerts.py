"""alerts.py: send a breach alert to Telegram, with the reasons attached (T24).

The threat engine calls this the moment a host crosses 70. The message carries the score, the
reasons that produced it, what was done about it and where the written report is, so whoever
gets it on their phone already knows what happened without opening a laptop.

**The bot token is never in the repo.** It is read from the environment, or from a git-ignored
`.env` file at the project root:

    FORTRESS_TELEGRAM_TOKEN=123456:AA...        from BotFather
    FORTRESS_TELEGRAM_CHAT=-1001234567890       the chat or group to send to

With no token configured, `send()` reports that it was skipped and the engine carries on: the
score, the block and the incident report do not depend on anyone's phone.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
API = "https://api.telegram.org"


def config(env_file=ENV_FILE):
    """{"token", "chat"} from the environment, falling back to .env. Empty strings if unset."""
    values = {}
    if Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return {"token": os.environ.get("FORTRESS_TELEGRAM_TOKEN", values.get("FORTRESS_TELEGRAM_TOKEN", "")),
            "chat": os.environ.get("FORTRESS_TELEGRAM_CHAT", values.get("FORTRESS_TELEGRAM_CHAT", ""))}


def compose(result, blocked=None, report=None):
    """The alert text for one scored host. Plain text, so no character needs escaping."""
    lines = [f"FORTRESS-NET BREACH: {result['host']} scored {result['score']}/100",
             f"host {result['host']} ({result.get('ip', '?')})", "", "Why:"]
    lines += [f"- {reason}" for reason in result["reasons"]]
    lines.append("")
    if blocked and not blocked.get("error"):
        lines.append(f"Action: cut off at {blocked['port']} on {blocked['switch']} "
                     f"for {blocked.get('seconds', 0):.0f} s")
    elif blocked:
        lines.append(f"Action: could not block ({blocked['error']})")
    else:
        lines.append("Action: none (blocking is switched off)")
    if report:
        lines.append(f"Report: {Path(report).relative_to(ROOT) if ROOT in Path(report).parents else report}")
    return "\n".join(lines)


def send(text, cfg=None, timeout=8):
    """Send one message. Returns Telegram's answer, {"skipped": ...} or {"error": ...}."""
    cfg = cfg or config()
    if not cfg["token"] or not cfg["chat"]:
        return {"skipped": "no FORTRESS_TELEGRAM_TOKEN/CHAT configured (see .env.example)"}
    data = urllib.parse.urlencode({"chat_id": cfg["chat"], "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"{API}/bot{cfg['token']}/sendMessage", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"Telegram refused it: HTTP {e.code} {e.read().decode(errors='replace')[:200]}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": str(e)}


def notify(result, blocked=None, report=None):
    """Compose and send. Returns (text, outcome) so the caller can log what it sent."""
    text = compose(result, blocked, report)
    return text, send(text)
