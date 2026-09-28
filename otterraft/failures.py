"""Classify why a Claude CLI run failed, so each cause gets the right recovery:

quota       -> this account is out of usage: cool it down (until its reset) and switch account
transient   -> provider overloaded / short rate limit / network: wait with backoff, same task
login       -> the account is logged out or its token is dead: park it until you log in again
bad_session -> --resume cannot continue this session: drop it and start the task fresh

Only short terminal messages are classified. A long failure text is the model's own prose
(a task *about* rate limiting that failed must not park an account)."""
import datetime as dt
import re

MAX_CLASSIFIED_LEN = 600

QUOTA_RE = re.compile(
    r"you(?:'|’)ve\s+hit\s+your\s+(?:\w+\s+)?limit|session\s+limit\s+(?:reached|exceeded)|"
    r"out\s+of\s+extra\s+usage|usage\s+limit\s+reached|usage\s+cap\s+reached|"
    r"(?:5|five)[-\s]?hour\s+limit\s+reached|(?:weekly|opus)\s+limit\s+reached|"
    r"credit\s+balance\s+is\s+too\s+low", re.I)
TRANSIENT_RE = re.compile(
    r"overloaded(?:_error)?|\b529\b|\b503\b|service\s+unavailable|rate_limit_error|"
    r"too\s+many\s+requests|\b429\b|temporarily\s+unavailable|try\s+again\s+later|high\s+demand|"
    r"ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|socket\s+hang\s+up|network\s+error|fetch\s+failed", re.I)
LOGIN_RE = re.compile(
    r"not\s+logged\s+in|please\s+(?:run\s+)?`?/?(?:claude\s+)?login|login\s+(?:required|expired)|run\s+`?/login|"
    r"invalid\s+api\s+key|authentication[_\s-](?:failed|error)|failed\s+to\s+authenticate|"
    r"(?:invalid|expired|revoked)[\s\S]{0,40}(?:bearer|oauth|access)\s+token|"
    r"(?:oauth|access)\s+token[\s\S]{0,40}(?:has\s+)?(?:expired|been\s+revoked)", re.I)
BAD_SESSION_RE = re.compile(
    r"no\s+conversation\s+found|previous_message_id|could\s+not\s+(?:find|resume|load)\s+(?:the\s+)?session|"
    r"session\s+(?:not\s+found|is\s+invalid)|invalid\s+session", re.I)
EPOCH_RE = re.compile(r"\|(\d{10})\b")
RESET_RE = re.compile(
    r"\bresets?\s+(?:at\s+|on\s+)?"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"(?:\s*\((?P<tz>[A-Za-z_]+/[A-Za-z_/]+|UTC)\))?", re.I)
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def classify(text, resumed=False, num_turns=None):
    """Return (kind, reset_epoch) with kind in quota|transient|login|bad_session|None."""
    text = (text or "").strip()
    if text and len(text) <= MAX_CLASSIFIED_LEN:
        # Order matters: "hit your limit" beats the generic 429 in the same message.
        if QUOTA_RE.search(text) or EPOCH_RE.search(text) and re.search(r"limit", text, re.I):
            return "quota", parse_reset(text)
        if LOGIN_RE.search(text):
            return "login", None
        if resumed and BAD_SESSION_RE.search(text):
            return "bad_session", None
        if TRANSIENT_RE.search(text):
            return "transient", None
    if resumed and num_turns == 0 and text and len(text) <= MAX_CLASSIFIED_LEN:
        return "bad_session", None  # the resume died before doing any work
    return None, None


def parse_reset(text, now=None):
    """Epoch of the reset time in a limit message ('…|1737000000', 'resets 3pm',
    'resets 3:30pm (Asia/Ho_Chi_Minh)', 'resets Oct 3, 9am'), or None."""
    m = EPOCH_RE.search(text or "")
    if m:
        return int(m.group(1))
    m = RESET_RE.search(text or "")
    if not m or (not m.group("ampm") and not m.group("m")):
        return None  # a bare number is too ambiguous to trust
    tz = None
    if m.group("tz"):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m.group("tz"))
        except Exception:  # Windows ships no IANA database; the CLI's own zone is the local one anyway
            tz = dt.timezone.utc if m.group("tz").upper() in ("UTC", "GMT", "ETC/UTC") else None
    # Without an explicit zone the CLI prints the machine's local time.
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    hour, minute = int(m.group("h")), int(m.group("m") or 0)
    if m.group("ampm"):
        hour = hour % 12 + (12 if m.group("ampm").lower() == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    if m.group("mon"):
        try:
            when = now.replace(month=MONTHS.index(m.group("mon")[:3].lower()) + 1,
                               day=int(m.group("day")), hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if when < now - dt.timedelta(days=1):
            when = when.replace(year=when.year + 1)
    else:
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= now:
            when += dt.timedelta(days=1)
    return int(when.timestamp())
