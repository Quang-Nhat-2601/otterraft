# 🦦 OtterRaft

**Your AI agents hold hands, so no task drifts away.**

Sea otters hold hands while they sleep so the current doesn't pull them apart; a group of them floating together is called a *raft*. OtterRaft does the same for your Claude Code accounts and agents. You hand it tasks, and it runs them unattended across your accounts. When one account runs out of usage, another picks up the same session. You follow everything from a live dashboard, including on your phone.

## What it does

- **Switches accounts for you.** Use several Claude Code accounts, each with its own login. When an account's 5-hour window gets close to full (90% by default), or hits its limit, OtterRaft moves to the next account. A task that was mid-way **carries its session over and continues** instead of starting again.
- **Runs unattended.** Claude runs headless in `--permission-mode auto`, so it never stops to ask for permission. Tools that get blocked are collected as *permission suggestions*: approve one once and it is not blocked again.
- **Lets Claude dispatch.** Claude Sonnet classifies each task with one lean call (about 7K input tokens and 2–3 seconds, measured), then sends it to the right worker:
  - **Claude agent** for work that edits code or runs commands. Tasks rated complexity ≤3 run on Sonnet, which saves your stronger model's quota for hard work.
  - **Claude quick answer** for text-only work such as summaries, translations and commit messages: about 7K input tokens instead of the 25K+ that even a trivial agent session costs.
  - **Local model** (optional, off by default) if you want simple work to stay on your machine.
- **Shows a live dashboard.** Watch each agent's tool calls and commands as they happen, follow progress through the agent's own todo list, and see usage and success rates per account and per model.
- **Reports after every task.** The report says how the agent understood the request, what it produced, which files changed, and the result of your verify command. It also includes a **checklist of test cases for you to try**.
- **Gives every task its own git branch.** Tasks run in a git worktree on `otterraft/task-<id>`. Several tasks can run on one repo in parallel, and your checkout is untouched until you press **Merge**.
- **Recovers on its own.**
  - An overloaded provider means wait and retry.
  - An account out of usage means switch accounts.
  - A logged-out account is set aside and you are told.
  - A session that cannot be resumed is started over.
- **Learns.**
  - A **Reflection Coach** periodically reviews recent tasks, finds problems that repeat, and proposes evidence-backed edits to `CLAUDE.md` or new skills. You approve each one, and you can roll it back.
  - Your feedback when you reject a result becomes a lesson for later tasks.
  - The router learns which worker succeeds at which kind of task.
- **Notifies your phone** through [ntfy](https://ntfy.sh) when a task finishes, needs your answer, stalls, or an account is switched.

Python 3.10+ and the standard library only. No dependencies. Runs on Windows, macOS and Linux, with Claude Code installed either way (native installer or npm).

## How it compares

Several tools cover **part** of this. As far as we know, none combines all of it:

| Tool | Does | Doesn't |
|---|---|---|
| vibe-kanban | Kanban board, runs several coding agents in parallel | Account switching, dispatching, self-improvement |
| claude-squad | Manages many Claude sessions in tmux and worktrees | Usage dashboard, automatic routing |
| ccusage | Usage and cost statistics from Claude Code logs | Read-only: runs nothing |
| claude-code-router | Sends Claude Code requests to other models (incl. Ollama) | Routes per request, not per task; no dashboard or reports |
| [Paperclip](https://github.com/paperclipai/paperclip) | Full "AI company" control plane: org charts, budgets, governance | Automatic account failover (it waits for the quota to reset), per-task routing to cheaper workers |

ccusage works well alongside OtterRaft if you want more statistics.

## Architecture

```
             ┌──────────── Dashboard (web, mobile) ────────────┐
             │ Tasks · live log · test cases · usage · brain   │
             └───────────────▲──────────────┬──────────────────┘
                     SSE live│              │REST (token)
┌────────────────────────────┴──────────────▼─────────────────────────┐
│ OtterRaft (otterraft serve)                                         │
│                                                                     │
│  Brain: Sonnet, one lean call (no tools/skills/MCP, ~7K tokens)     │
│     fallback: local model → keyword rules; + learned success stats  │
│     │                                                               │
│     ├── Claude agent ── Account pool ── acc1 (CLAUDE_CONFIG_DIR=…)  │
│     │   claude -p --output-format stream-json --permission-mode auto│
│     │   git worktree per task · sonnet if complexity ≤3             │
│     │   limit/utilization ≥ 90%? → cooldown → handoff → --resume    │
│     │                                                               │
│     ├── Claude quick ── one lean call, text-only answers            │
│     │      needs files after all → escalate to the agent            │
│     │                                                               │
│     └── Local (optional) ── Ollama / OpenAI-compatible server       │
│            fail → escalate to Claude quick                          │
│                                                                     │
│  After each task: parse report → commit branch → verify_cmd →       │
│                   lessons → notify        Reflection Coach: weekly  │
│  SQLite: tasks · events · usage · lessons · proposals · permissions │
└─────────────────────────────────────────────────────────────────────┘
```

## Getting started

### 1. Install and create a config

```bash
git clone https://github.com/Quang-Nhat-2601/otterraft && cd otterraft
pip install .                 # or: pipx install .   (python -m otterraft also works without installing)
otterraft init                # writes otterraft.json from the example
```

Edit `otterraft.json` and list your accounts. Each account has its own config directory:

```json
"accounts": [
  {"name": "personal", "config_dir": "~/.claude-personal", "priority": 1, "max_parallel": 1},
  {"name": "work",     "config_dir": "~/.claude-work",     "priority": 2, "max_parallel": 1}
]
```

`"config_dir": null` means the default `~/.claude`, i.e. the account you are already logged into.

#### On Windows

Native Windows is supported and tested (CI runs Windows, Linux and macOS); WSL works exactly like Linux. Two things to know:

- **Application Control / Smart App Control** can block the small unsigned launchers pip generates (`pip.exe`, `otterraft.exe`) with *"An Application Control policy has blocked this file"*. Run everything through Python instead: `python -m pip install -e .` and `python -m otterraft <command>`. If the venv's `python.exe` is blocked too, skip the venv and use the signed launcher: `py -m pip install --user -e .` and `py -m otterraft <command>`.
- **Claude Code from npm** is a `claude.cmd` wrapper. OtterRaft finds the `cli.js` behind it and runs it with Node directly, so no argument goes through `cmd.exe`; if it can't find it, it runs the wrapper itself, which also works. `otterraft doctor` tells you which command it will use. With the native Windows installer there is nothing to do.

### 2. Log each account in (once)

```bash
otterraft login personal      # opens Claude Code: type /login, sign in, then /exit
otterraft login work
```

### 3. Your skills, CLAUDE.md, hooks and MCP servers

Agents under OtterRaft are full Claude Code sessions. They **use your skills** from `~/.claude/skills` and the project's `.claude/skills`, and they read your CLAUDE.md, hooks, plugins and MCP servers too.

Claude Code only reads skills and settings from an account's own config directory. So `serve` and `login` link `skills/`, `agents/`, `commands/`, `plugins/`, `CLAUDE.md` and `settings.json` from `~/.claude` into every account, and copy your `mcpServers` (the login itself is never touched). If an account already has its own copy of one of these, OtterRaft leaves it alone and `doctor` tells you, so you can merge by hand. On Windows without symlink rights, files are shared as hard links, which split when an editor saves the shared file by replacing it; while `serve` runs it links them again within a minute and keeps the stale copy as `*.otterraft-bak`.

### 4. Check and run

```bash
otterraft doctor              # CLI, logins, shared config, local models
otterraft serve               # prints the dashboard link, including its access token
```

### Optional: local models

**You don't need them.** Sonnet dispatches tasks and answers text-only ones for very little quota: a classification is about 7K input tokens, a small fraction of a coding task. On a CPU-only machine it is also faster than a local model (2–3 s against roughly 5–30 s) and doesn't take 13 GB of RAM.

Turn local models on (`"local": {"enabled": true}`) when you need data to stay on your machine, need to work offline, or have lots of repetitive text work. They only do simple, text-only work: summaries, translations, explanations, commit messages, regexes. Anything that reads or changes code still goes to Claude.

**On CPU only**, speed depends almost entirely on **memory bandwidth**, because every token reads all of the model's *active* weights. Mixture-of-Experts models (large on disk, but only ~3–4B parameters active per token) are therefore much faster than dense models of the same size. For **32 GB RAM without a GPU**:

| Model (Ollama tag) | Size | Type | Role | Notes |
|---|---|---|---|---|
| `gpt-oss:20b` **(default)** | ~13 GB | MoE, ~3.6B active | simple + router | Leaves room for a browser and an IDE. Keep `"think": "low"` |
| `qwen3.6:35b-a3b` | ~23 GB | MoE, ~3B active | simple | Better answers, but tight on 32 GB: only with few other apps open |
| `gemma4:e4b` or another 3–4B model | small | dense | router | Only if you want very fast classification; one shared model is usually enough |

Avoid dense 27–32B models on CPU: expect a few tokens per second.

```bash
ollama pull gpt-oss:20b
otterraft bench                                  # measure on your own machine
otterraft bench -m gpt-oss:20b,qwen3.6:35b-a3b
```

`bench` times three real jobs: classifying a task, summarizing a paragraph, and writing a commit message. It reports model load time, prompt and generation speed, and whether the model is fast enough. If classification takes over 15 seconds, set `router.use_llm_classifier: false` and let keyword rules do the fallback routing.

Use **one model for every role**: two models in RAM keep evicting each other. Roles are `simple` (does tasks) and `router` (classifies):

```json
"local": {
  "enabled": true,
  "models": [
    {"name": "gpt-oss:20b", "roles": ["simple", "router"], "think": "low"},
    {"name": "my-model", "provider": "openai", "url": "http://localhost:1234", "roles": ["simple"]}
  ]
}
```

With local models on, they get simple text tasks first. If a local model fails, the task goes to a Claude quick answer. If every Claude account is out of usage, the brain classifies with the local model, then with keyword rules.

## Using it

**From the dashboard:** describe the task, pick a project folder and optionally a verify command (e.g. `pytest -q`), then press **Start task**.

**From the terminal:**

```bash
otterraft add "Fix the login 500 when the email has spaces, and add a test" -w ~/code/app -v "pytest -q"
otterraft add "Summarize this CHANGELOG" -a quick
git diff | otterraft add - -t "Write a commit message"
otterraft accounts            # account status and cooldowns
```

Tasks can be written in any language. The keyword fallback also understands Vietnamese.

**A task's life**

`Queued` → `Running` (live log and progress bar) → `Ready for review` or `Needs your answer` → you **Accept** it (`Done`) or **Reject it with feedback**. A rejected task continues in the same session to fix its work, and your feedback becomes a lesson.

- If the agent can't decide something, the task moves to *Needs your answer* and shows its questions. Answer on the dashboard and it continues.
- After 5 minutes without activity a task is marked *stalled* and you get a notification.

**Git worktrees**

When the workdir is a git repo, the agent works on branch `otterraft/task-<id>` in its own folder under `~/.otterraft/worktrees/`. When it finishes, OtterRaft commits the changes to that branch, skipping `__pycache__`, `node_modules`, `.venv` and similar. On the dashboard you can:
- **Show the diff**.
- **Merge into the base branch**. This only runs when your checkout is on that branch with no uncommitted changes. On a conflict, OtterRaft aborts the merge and nothing changes.
- **Remove the worktree**.

Worktrees start from `HEAD`. A `CLAUDE.md` with uncommitted changes (for example one the Reflection Coach just updated) is copied into new worktrees so agents see it right away, and it is never committed on a task branch. Commit it yourself when you're happy with it.

If a project needs dependencies installed before tests can run, set `workspace.setup_cmd` (e.g. `npm ci`). When the workdir is not a git repo, the task runs in place, one task per folder at a time.

**When things go wrong**

| Problem | What OtterRaft does |
|---|---|
| Provider overloaded (529/503), short 429, network error | Waits 30 s, 60 s, 120 s… and retries the same task (up to 5 times) without switching accounts |
| Account out of usage ("hit your limit", "usage limit reached") | Rests the account until its reset time (read from the message or its usage data), hands the session to another account and continues |
| Account logged out / token expired | Rests the account 15 s first (the CLI sometimes reports this once on a valid token). A second failure within 10 minutes, or a third without a successful call in between, sets it aside and tells you to run `otterraft login <name>` (which puts it back); the task moves to another account |
| A session cannot be resumed | Drops that session and starts the task over, keeping your message |

Only short error messages printed by the CLI are classified, so a task *about* rate limiting that fails never locks an account by mistake.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `claude.permission_mode` | `auto` | `auto` = never asks, with a safety classifier. `acceptEdits` is stricter. Use `bypassPermissions` only in a sandbox or VM |
| `claude.switch_at_utilization` | `0.9` | Prefer another account once this one's 5-hour window is this full |
| `claude.handoff_on_limit` | `true` | Carry the session over to the next account and `--resume` it |
| `claude.allowed_tools` | `[]` | Allowlist; grows when you approve permission suggestions |
| `claude.max_turns` / `max_budget_usd` | 80 / 0 | Safety limits per run |
| `claude.transient_retries` | 5 | Retries when the provider is overloaded |
| `claude.model` | `""` | Model for hard agent tasks (`""` = the account's default) |
| `claude.light_model` / `light_max_complexity` | `sonnet` / 3 | Agent tasks rated at most this complexity run on the light model |
| `brain.provider` / `brain.model` | `claude` / `sonnet` | Who classifies tasks: `claude` (one lean call), `local`, or `keywords` |
| env `OTTERRAFT_BRAIN_API_KEY` | unset | Set it and the brain classifies with this API key (billed per token) instead of a subscription account. Agents and quick answers still use subscriptions, and never see this variable. If the key fails, the brain says so on the `serve` console and uses subscription accounts for 10 minutes |
| `quick.enabled` / `quick.model` / `quick.max_complexity` | true / `sonnet` / 3 | Text-only tasks get one lean Claude call instead of an agent session |
| `local.enabled` | false | Use local models for simple text tasks |
| `router.local_categories` | summarize, translate… | Task types that count as text-only (sent to quick or local) |
| `router.local_max_complexity` | 2 | Highest complexity (1–5) a local model may take |
| `router.min_local_success` | 0.6 | Below this success rate, a worker stops getting that task type |
| `workspace.use_worktrees` | true | One git worktree and branch per task |
| `workspace.setup_cmd` | `""` | Runs once in each new worktree, e.g. `npm ci` |
| `learning.coach_every_days` / `coach_min_tasks` | 7 / 5 | The Reflection Coach runs every 7 days if at least 5 tasks finished since its last run. `0` = only when you press the button |
| `learning.auto_approve_llm_lessons` | false | Lessons written by a model wait for your approval |
| `shared_config_dir` | `~/.claude` | Skills, agents, commands, plugins, CLAUDE.md, settings.json (hooks) and MCP servers here are shared with every account |
| `notify.ntfy_url` | `""` | e.g. `https://ntfy.sh/your-secret-topic`; install the ntfy app on your phone |
| `auto_accept` | false | Mark tasks whose verify passed as done without review |
| `claude_bin` | `claude` | The Claude Code CLI, looked up on `PATH` (finds npm's `claude.cmd` on Windows too). A full path, or a list such as `["python", "wrapper.py"]` to run a wrapper |
| `auth_token` | generated | The API always needs a token. Left empty, one is generated and kept in `~/.otterraft/token`; `serve` prints a link that includes it |

## The dashboard on your phone

The dashboard listens on `127.0.0.1` by default. To open it from your phone:

1. Take the token from the link `serve` prints (or from `~/.otterraft/token`).
2. Install [Tailscale](https://tailscale.com) on your computer and your phone, and set `"host": "0.0.0.0"`.
3. Open `http://<your-tailscale-machine>:8787/?token=...`.

Never expose this port to the public internet: anyone who reaches the dashboard can give agents work on your machine.

## How it learns

1. **Reflection Coach** (Brain tab). The idea comes from [Paperclip](https://github.com/paperclipai/paperclip).
   - Every 7 days, or when you press **Run reflection now**, Claude reads the recent task history: prompts, results, failed verify commands, tool errors, and your feedback on rejected work.
   - It looks for **problems that repeat** and proposes small, durable fixes: a section added to CLAUDE.md (global or per project), or a new or updated skill.
   - Every proposal must cite **the tasks that show the problem**. Proposals without evidence are dropped.
   - Proposals stay small: CLAUDE.md grows by at most 20%, a skill is at most 15 KB.
   - The coach runs with **read-only** tools and writes nothing. Only when you press **Approve and apply** does OtterRaft write the file, after taking a backup. **Roll back** restores the previous version.
   - Skills and CLAUDE.md are shared by all accounts, so an improvement applies everywhere.
2. **Lessons.** Your feedback on rejected work and failed verify commands are added to the prompts of later tasks. Enable, disable, delete or add lessons in the Brain tab.
3. **The router learns.** Success rates per (task type, worker) decide who gets the next task of that type. Your reviews and verify commands decide what counts as success.
4. **Permissions.** Tools that auto mode keeps blocking become suggestions. Approve one once and it is written to `allowed_tools`.

## Good to know

- **Terms of use.** Check Anthropic's terms regarding multiple accounts yourself. OtterRaft assumes each account is yours and used legitimately (for example a personal and a work account).
- **Session handoff** copies the transcript `projects/<project>/<session>.jsonl` into the other account's config directory. If that fails, the task starts over, with a note to check the working tree for partial progress.
- **On macOS**, logins live in the Keychain, so `doctor` may show `??` even when you are logged in. Running a task tells you for sure.
- **Auto mode still runs commands on your machine.** Use git, and set a `verify_cmd` on important projects so every result gets checked.
- **`verify_cmd` is a shell command run as you.** Anyone with the token can run commands, so treat the token like a password.
- **Tasks without a workdir** run in their own folder, `~/.otterraft/workspaces/task-<id>`.
- **Permission suggestions** never offer blanket rules for dangerous commands (`rm`, `sudo`, `curl`, `git push`…) or compound ones (`|`, `&&`, `;`). Those stay under auto mode's per-call judgement.
- OtterRaft is an independent project, not affiliated with Anthropic. "Claude" and "Claude Code" are Anthropic's trademarks.

## Development

```bash
python -m unittest discover tests      # end-to-end, against a fake claude CLI and a fake Ollama (python3 on some Linux/macOS)
```

Code map:

- `otterraft/runner.py`: scheduler, running tasks, verify, finishing, failure recovery
- `otterraft/agents/claude.py`: headless Claude Code, stream-json parsing, session handoff, lean calls
- `otterraft/agents/local.py`: Ollama and OpenAI-compatible servers
- `otterraft/router.py`: the brain: classification and routing
- `otterraft/coach.py`: Reflection Coach
- `otterraft/workspace.py`: git worktrees, commit, merge
- `otterraft/failures.py`: classifying CLI failures, parsing reset times
- `otterraft/learning.py`: lessons, permission suggestions, feedback
- `otterraft/accounts.py`: account pool, cooldowns, shared config
- `otterraft/server.py` + `otterraft/static/index.html`: API, SSE, dashboard

## License

[MIT](LICENSE)
