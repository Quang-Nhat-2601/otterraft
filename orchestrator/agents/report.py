"""The end-of-task report protocol every agent is asked to follow, and its parser."""
import json
import re

REPORT_INSTRUCTIONS = """
You are running unattended under an orchestrator. Nobody is watching the terminal.
- Keep a todo list (TodoWrite / task tools) for any multi-step work and update it as you go;
  the orchestrator turns it into a progress bar.
- Do not stop to ask questions. Make a reasonable decision and write it down in the report.
  Only if you truly cannot proceed, set status "needs_input" and put your questions in "questions".
- When you finish, end your final message with exactly one fenced block like this:

```orchestrator-report
{
  "status": "done | partial | needs_input | failed",
  "summary": "one paragraph: what you did",
  "input": "how you understood the request",
  "output": ["concrete results: files changed, commands, artifacts"],
  "test_cases": [
    {"title": "what the user should verify", "steps": ["step 1", "step 2"], "expected": "expected result"}
  ],
  "questions": [],
  "notes": "risks, follow-ups, anything the user must know"
}
```
""".strip()

_BLOCK = re.compile(r"```orchestrator-report\s*(\{.*?\})\s*```", re.S)
_ANY_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_report(text):
    """Extract the report dict from an agent's final text. Returns None when absent."""
    if not text:
        return None
    for rx in (_BLOCK, _ANY_JSON):
        for m in reversed(list(rx.finditer(text))):
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            if isinstance(data, dict) and ("summary" in data or "status" in data):
                data.setdefault("test_cases", [])
                data.setdefault("output", [])
                return data
    return None


def strip_report(text):
    return _BLOCK.sub("", text or "").strip()
