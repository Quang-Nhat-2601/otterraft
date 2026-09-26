# AI Orchestrator

Một "bộ não điều phối" chạy trên máy của bạn:

- **Tự đổi tài khoản Claude**: dùng nhiều tài khoản Claude Code. Khi một tài khoản gần đầy cửa sổ 5 giờ (mặc định 90%) hoặc chạm giới hạn, nó chuyển sang tài khoản khác. Session đang chạy dở được **mang sang tài khoản mới và chạy tiếp**, không phải làm lại từ đầu.
- **Auto mode thật sự**: Claude chạy headless với `--permission-mode auto`, không dừng lại xin quyền. Tool nào bị chặn sẽ được gom thành "đề xuất quyền"; bạn bấm duyệt một lần, những lần sau không bị chặn nữa.
- **Giao việc cho AI local**: task đơn giản (tóm tắt, dịch, giải thích, viết commit message, regex, snippet…) được chuyển cho model local (Ollama / LM Studio / llama.cpp). Nếu model local làm hỏng, task tự động chuyển lên Claude.
- **Dashboard trực tiếp**: xem AI đang làm gì theo thời gian thực (tool nào, lệnh gì), tiến độ theo todo list, usage và tỉ lệ thành công của từng tài khoản và từng model.
- **Báo cáo sau mỗi task**: AI hiểu input là gì, output ra sao, file nào thay đổi, kết quả verify tự động, và **danh sách test case bạn cần kiểm tra** (có checkbox).
- **Tự học**: sau mỗi task, hệ thống rút ra bài học (lệnh nào chạy được, quy ước của dự án, lý do bạn từ chối kết quả…) và chèn vào prompt của các task sau. Router cũng học từ tỉ lệ thành công: loại việc nào model local hay làm hỏng thì từ đó giao cho Claude.
- **Báo lên điện thoại** qua [ntfy](https://ntfy.sh) khi task xong, cần bạn trả lời, bị treo, hoặc khi đổi tài khoản.

Chỉ cần Python 3.10+ và thư viện chuẩn, không phải cài package nào.

## Đã có tool nào như vậy chưa?

Theo tôi biết, có vài tool làm được **từng phần**, nhưng chưa tool nào gộp đủ các nhu cầu trên:

| Tool | Làm được | Còn thiếu |
|---|---|---|
| vibe-kanban | Bảng kanban, chạy nhiều coding agent song song | Không tự đổi tài khoản, không có router local, không tự học |
| claude-squad | Quản lý nhiều session Claude trong tmux/worktree | Không có dashboard usage, không có auto-routing |
| ccusage | Thống kê usage/cost từ log của Claude Code | Chỉ để xem, không điều phối |
| claude-code-router | Chuyển request của Claude Code sang model khác (kể cả Ollama) | Route theo từng request, không theo task; không có dashboard hay báo cáo |

Vì vậy tôi build cái này. Bạn vẫn có thể dùng chung với ccusage nếu muốn xem thêm số liệu.

## Kiến trúc

```
             ┌──────────── Dashboard (web, mobile) ────────────┐
             │ Tasks · live log · test cases · usage · brain   │
             └───────────────▲──────────────┬──────────────────┘
                     SSE live│              │REST
┌────────────────────────────┴──────────────▼─────────────────────────┐
│ Orchestrator (python -m orchestrator serve)                         │
│                                                                     │
│  Router (brain) ── classify: local LLM / heuristic + learned stats  │
│     │                                                               │
│     ├── Claude runner ── Account pool ── acc1 (CLAUDE_CONFIG_DIR=…) │
│     │   claude -p --output-format stream-json --permission-mode auto│
│     │   limit/utilization ≥ 90%? → cooldown → handoff → --resume    │
│     │                                                               │
│     └── Local runner ── Ollama / OpenAI-compatible server           │
│            fail → escalate to Claude                                │
│                                                                     │
│  After each task: parse report → git status → verify_cmd →          │
│                   reflect (lessons) → notify                        │
│  SQLite: tasks · events · usage · lessons · permission suggestions  │
└─────────────────────────────────────────────────────────────────────┘
```

## Cài đặt

### 1. Clone và tạo config

```bash
git clone <repo> && cd Nhat
python3 -m orchestrator init          # tạo orchestrator.json từ file mẫu
```

Sửa `orchestrator.json`, khai báo hai tài khoản. Mỗi tài khoản dùng một thư mục config riêng:

```json
"accounts": [
  {"name": "personal", "config_dir": "~/.claude-personal", "priority": 1, "max_parallel": 1},
  {"name": "work",     "config_dir": "~/.claude-work",     "priority": 2, "max_parallel": 1}
]
```

`config_dir: null` nghĩa là dùng `~/.claude` mặc định (tài khoản bạn đang đăng nhập sẵn).

### 2. Đăng nhập từng tài khoản (chỉ làm một lần)

```bash
python3 -m orchestrator login personal   # mở Claude Code → gõ /login → đăng nhập → /exit
python3 -m orchestrator login work
```

### 3. (Tuỳ chọn) Cài AI local

```bash
# https://ollama.com/download
ollama pull qwen2.5-coder:7b      # máy yếu hơn: qwen2.5-coder:3b / llama3.2:3b
```

Bạn có thể khai báo nhiều model, mỗi model đảm nhận một vai trò (`roles`):

```json
"local": {
  "models": [
    {"name": "qwen2.5-coder:7b", "roles": ["simple"]},
    {"name": "llama3.2:3b", "roles": ["router", "reflect"]},
    {"name": "my-model", "provider": "openai", "url": "http://localhost:1234", "roles": ["simple"]}
  ]
}
```

- `simple`: làm các task đơn giản
- `router`: phân loại task (loại việc, độ khó)
- `reflect`: rút bài học sau mỗi task

Không có model local cũng được: mọi task sẽ đi Claude, router dùng heuristic, và bài học được rút theo luật.

### Skills, CLAUDE.md, hooks, MCP của bạn

Claude chạy dưới orchestrator vẫn là Claude Code đầy đủ, nên nó **tự dùng skills** trong `~/.claude/skills` và `.claude/skills` của dự án, cùng CLAUDE.md, hooks, plugins và MCP servers.

Riêng các tài khoản có `config_dir` riêng: Claude Code chỉ đọc skills và cấu hình trong thư mục đó. Vì vậy khi chạy `serve` hoặc `login`, orchestrator tạo symlink `skills/`, `agents/`, `commands/`, `plugins/`, `CLAUDE.md`, `settings.json` từ `~/.claude` sang từng tài khoản, và chép phần `mcpServers` (không đụng tới thông tin đăng nhập). Nếu tài khoản đã có file riêng, orchestrator giữ nguyên và báo trong `doctor` để bạn tự gộp.

Model local (Ollama) thì **không** dùng được skills: nó chỉ trả lời văn bản, không có tool để đọc file hay chạy lệnh.

### 4. Kiểm tra rồi chạy

```bash
python3 -m orchestrator doctor    # kiểm tra CLI, đăng nhập, Ollama, model
python3 -m orchestrator serve     # mở http://127.0.0.1:8787
```

## Sử dụng

**Từ dashboard**: nhập mô tả, thư mục dự án, và lệnh verify (vd `pytest -q`) rồi bấm "Giao việc".

**Từ terminal**:

```bash
python3 -m orchestrator add "Sửa bug login trả 500 khi email có dấu cách, thêm test" -w ~/code/app -v "pytest -q"
python3 -m orchestrator add "Tóm tắt file CHANGELOG này" -a local
git diff | python3 -m orchestrator add - -t "Viết commit message"
python3 -m orchestrator accounts    # xem trạng thái các tài khoản
```

**Vòng đời một task**

`Chờ` → `Đang chạy` (live log + thanh tiến độ) → `Chờ duyệt` hoặc `Cần bạn trả lời` → bạn **Chấp nhận** (`Xong`) hoặc **Từ chối kèm nhận xét**. Khi bị từ chối, AI tiếp tục đúng session cũ để sửa, và hệ thống ghi lại bài học.

- Nếu AI không thể tự quyết, task chuyển sang "Cần bạn trả lời" và hiện câu hỏi. Bạn trả lời ngay trên dashboard, AI chạy tiếp.
- Nếu 5 phút không có hoạt động, task bị đánh dấu *stalled* và bạn nhận thông báo.
- Mỗi thư mục dự án chỉ có một task chạy tại một thời điểm, để hai AI không ghi đè lên nhau.

## Cấu hình quan trọng

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `claude.permission_mode` | `auto` | `auto` = không hỏi quyền, có classifier an toàn. `acceptEdits` chặt hơn. `bypassPermissions` chỉ nên dùng trong sandbox/VM. |
| `claude.switch_at_utilization` | `0.9` | Khi cửa sổ 5 giờ của tài khoản đạt mức này, ưu tiên tài khoản khác |
| `claude.handoff_on_limit` | `true` | Mang session sang tài khoản mới và `--resume` |
| `claude.allowed_tools` | `[]` | Allowlist; tự cập nhật khi bạn duyệt đề xuất quyền |
| `claude.max_turns` / `max_budget_usd` | 80 / 0 | Giới hạn an toàn cho mỗi lần chạy |
| `router.local_categories` | summarize, translate… | Các loại việc được phép giao cho local |
| `router.local_max_complexity` | 2 | Độ khó tối đa (1–5) được giao cho local |
| `router.min_local_success` | 0.6 | Nếu tỉ lệ thành công của local thấp hơn mức này, loại việc đó chuyển sang Claude |
| `notify.ntfy_url` | "" | vd `https://ntfy.sh/ten-bi-mat-cua-ban`; cài app ntfy trên điện thoại để nhận thông báo |
| `auto_accept` | false | true = task verify PASS thì tự chuyển sang Xong |
| `auth_token` | tự sinh | API luôn yêu cầu token. Để trống thì hệ thống tự sinh và lưu ở `~/.ai-orchestrator/token`; `serve` in ra link có sẵn token |
| `shared_config_dir` | `~/.claude` | Skills, agents, commands, plugins, CLAUDE.md, settings.json (hooks) và MCP servers ở đây được liên kết sang mọi tài khoản |
| `learning.auto_approve_llm_lessons` | false | Bài học do model tự rút ra phải chờ bạn duyệt |

## Xem dashboard từ điện thoại

Dashboard mặc định chỉ nghe ở `127.0.0.1`. Để xem từ điện thoại:

1. Lấy token trong link mà `serve` in ra (hoặc trong file `~/.ai-orchestrator/token`).
2. Cài [Tailscale](https://tailscale.com) trên cả máy tính và điện thoại, đặt `"host": "0.0.0.0"`.
3. Mở `http://<tên-máy-tailscale>:8787/?token=...`.

Đừng mở port này ra Internet công cộng: ai vào được dashboard là giao được việc cho AI chạy trên máy bạn.

## Tự học hoạt động thế nào

1. **Bài học** (tab Brain): sau mỗi task, model `reflect` đọc prompt, report, lỗi tool, kết quả verify và nhận xét của bạn, rồi rút ra 0–4 bài học cụ thể. Bài học có phạm vi theo dự án hoặc toàn cục. Khi có task mới, những bài học liên quan nhất (theo từ khoá, phạm vi và điểm) được chèn vào system prompt.
2. **Điểm**: task được chấp nhận thì bài học của nó được cộng điểm; bị từ chối thì trừ điểm. Nhận xét khi bạn từ chối trở thành bài học có điểm cao. Bạn có thể bật/tắt/xoá hoặc tự thêm bài học (vd "Dự án này dùng pnpm").
3. **Router học**: tỉ lệ thành công theo từng (loại việc, agent) quyết định lần sau giao cho ai.
4. **Quyền**: tool bị auto mode chặn nhiều lần sẽ thành đề xuất. Bạn duyệt một lần là nó được ghi vào `allowed_tools`, giúp AI làm độc lập hơn.

## Lưu ý

- **Điều khoản sử dụng**: hãy tự kiểm tra điều khoản của Anthropic về việc dùng nhiều tài khoản. Tool này giả định mỗi tài khoản là của bạn và được dùng hợp lệ (vd một tài khoản cá nhân và một tài khoản công ty).
- Handoff session hoạt động bằng cách copy transcript `projects/<dự án>/<session>.jsonl` sang thư mục config của tài khoản kia. Nếu copy thất bại, task chạy lại từ đầu kèm ghi chú "kiểm tra working tree để thấy tiến độ dở dang".
- Trên macOS, token đăng nhập nằm trong Keychain, nên `doctor` có thể báo `??` dù bạn đã đăng nhập. Chạy thử một task là biết.
- Auto mode vẫn có thể chạy lệnh trên máy bạn. Nên dùng git, và với dự án quan trọng thì đặt `verify_cmd` để luôn có kiểm tra tự động.
- `verify_cmd` là lệnh shell chạy thẳng trên máy bạn. Ai có token là chạy được lệnh, nên hãy giữ token như mật khẩu.
- Task không ghi workdir sẽ chạy trong thư mục riêng `~/.ai-orchestrator/workspaces/task-<id>`.
- Đề xuất quyền không bao giờ gợi ý cho phép hàng loạt các lệnh nguy hiểm (`rm`, `sudo`, `curl`, `git push`…) hay lệnh ghép (`|`, `&&`, `;`). Những lệnh đó vẫn do auto mode xét từng lần.

## Phát triển

```bash
python3 -m unittest discover tests     # e2e với claude CLI giả và Ollama giả
```

Cấu trúc code:

- `orchestrator/runner.py`: scheduler, chạy task, verify, hoàn tất
- `orchestrator/agents/claude.py`: chạy Claude Code headless, parse stream-json, handoff
- `orchestrator/agents/local.py`: Ollama và server tương thích OpenAI
- `orchestrator/router.py`: phân loại và định tuyến task
- `orchestrator/learning.py`: bài học, đề xuất quyền, feedback
- `orchestrator/accounts.py`: pool tài khoản, cooldown, phát hiện giới hạn
- `orchestrator/server.py` + `static/index.html`: API, SSE, dashboard
