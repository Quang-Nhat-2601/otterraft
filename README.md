# AI Orchestrator

Một "bộ não điều phối" chạy trên máy của bạn:

- **Tự đổi tài khoản Claude**: dùng nhiều tài khoản Claude Code. Khi một tài khoản gần đầy cửa sổ 5 giờ (mặc định 90%) hoặc chạm giới hạn, nó chuyển sang tài khoản khác. Session đang chạy dở được **mang sang tài khoản mới và chạy tiếp**, không phải làm lại từ đầu.
- **Auto mode thật sự**: Claude chạy headless với `--permission-mode auto`, không dừng lại xin quyền. Tool nào bị chặn sẽ được gom thành "đề xuất quyền"; bạn bấm duyệt một lần, những lần sau không bị chặn nữa.
- **Claude điều phối (Sonnet)**: mỗi task được Sonnet phân loại bằng một lần gọi tinh gọn (~2.500 token, ~2–3 giây), rồi giao cho đúng người làm:
  - **Claude agent** cho việc cần sửa code hoặc chạy lệnh. Task có độ khó ≤3 chạy bằng Sonnet để tiết kiệm quota của model mạnh hơn.
  - **Claude trả lời nhanh** cho việc chỉ cần văn bản (tóm tắt, dịch, commit message…): khoảng 2.500 token, thay vì 40.000+ token của một phiên agent.
  - **AI local** (tuỳ chọn, tắt mặc định) nếu bạn muốn giữ việc đơn giản trên máy.
- **Dashboard trực tiếp**: xem AI đang làm gì theo thời gian thực (tool nào, lệnh gì), tiến độ theo todo list, usage và tỉ lệ thành công của từng tài khoản và từng model.
- **Báo cáo sau mỗi task**: AI hiểu input là gì, output ra sao, file nào thay đổi, kết quả verify tự động, và **danh sách test case bạn cần kiểm tra** (có checkbox).
- **Mỗi task một nhánh git riêng**: task chạy trong git worktree trên nhánh `orch/task-<id>`. Nhiều task chạy song song trên cùng repo, và checkout của bạn không bị đụng tới cho tới khi bạn bấm **Merge**.
- **Tự phục hồi**: server quá tải thì chờ rồi thử lại; hết quota thì đổi tài khoản; tài khoản bị đăng xuất thì tạm gác và báo bạn; session không resume được thì chạy lại từ đầu.
- **Tự học**:
  - **Reflection Coach** định kỳ đọc lại các task, tìm lỗi lặp lại và đề xuất sửa CLAUDE.md hoặc tạo skill, mỗi đề xuất kèm bằng chứng. Bạn duyệt trước khi áp dụng, và có thể hoàn tác.
  - Nhận xét khi bạn từ chối kết quả trở thành bài học cho các task sau.
  - Router học từ tỉ lệ thành công: loại việc nào model local hay làm hỏng thì từ đó giao cho Claude.
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
│  Brain: Sonnet, one lean call (no tools/skills/MCP, ~2.5K tokens)   │
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

**Mặc định không cần.** Claude Sonnet điều phối và trả lời các task văn bản với rất ít quota: một lần phân loại khoảng 2.500 token, bằng dưới 1% một task code. Trên máy chỉ có CPU, Sonnet cũng nhanh hơn model local (2–3 giây so với 5–30 giây) và không chiếm 13GB RAM. Chỉ bật AI local (`"local": {"enabled": true}`) nếu bạn muốn dữ liệu không rời máy, cần làm việc offline, hoặc có rất nhiều việc văn bản lặp lại.

AI local chỉ làm việc **đơn giản, chỉ ra văn bản**: tóm tắt, dịch, giải thích, commit message, regex, phân loại task. Việc cần đọc/sửa code vẫn giao cho Claude.

**Máy chỉ có CPU:** tốc độ phụ thuộc gần như hoàn toàn vào **băng thông RAM**, vì mỗi token model phải đọc lại toàn bộ trọng số *đang hoạt động*. Vì vậy trên CPU, model **MoE** (tổng tham số lớn nhưng mỗi token chỉ dùng ~3–4 tỷ) nhanh hơn nhiều so với model dense cùng dung lượng.

Gợi ý cho **32GB RAM, không GPU**:

| Model (tag Ollama) | Dung lượng | Loại | Vai trò | Ghi chú |
|---|---|---|---|---|
| `gpt-oss:20b` **(mặc định)** | ~13 GB | MoE, ~3.6B tham số hoạt động | simple + router | Còn dư RAM cho trình duyệt/IDE. Đặt `"think": "low"` để không tốn thời gian suy nghĩ |
| `qwen3.6:35b-a3b` | ~23 GB | MoE, ~3B hoạt động | simple | Chất lượng tốt hơn, nhưng sát giới hạn 32GB: chỉ nên dùng khi máy không mở nhiều ứng dụng khác |
| `gemma4:e4b` hoặc một model 3–4B | nhỏ | dense | router | Chỉ cần nếu muốn phân loại cực nhanh; thường dùng chung một model là đủ |

Không nên dùng model dense 27–32B trên CPU: sẽ chỉ được vài token/giây.

```bash
ollama pull gpt-oss:20b
python3 -m orchestrator bench                      # đo trên chính máy bạn
python3 -m orchestrator bench -m gpt-oss:20b,qwen3.6:35b-a3b
```

`bench` chạy thử ba việc thật (phân loại task, tóm tắt tiếng Việt, viết commit message). Nó in ra thời gian nạp model, tốc độ đọc prompt và tốc độ sinh token, rồi kết luận model có đủ nhanh không. Nếu phân loại mất hơn 15 giây, hãy đặt `router.use_llm_classifier: false` để router dùng luật từ khoá.

Nên **dùng một model cho mọi vai trò**: hai model cùng nằm trong RAM sẽ đẩy nhau ra ngoài. Khai báo (`roles`: `simple` làm task, `router` phân loại):

```json
"local": {
  "models": [
    {"name": "gpt-oss:20b", "roles": ["simple", "router"], "think": "low"},
    {"name": "my-model", "provider": "openai", "url": "http://localhost:1234", "roles": ["simple"]}
  ]
}
```

Khi bật AI local, nó được ưu tiên cho task văn bản đơn giản. Nếu nó làm hỏng, task chuyển sang Claude trả lời nhanh. Nếu mọi tài khoản Claude đều hết quota, bộ não dùng model local để phân loại, rồi đến luật từ khoá.

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

**Git worktree**

Nếu workdir là git repo, Claude làm trên nhánh `orch/task-<id>` trong thư mục riêng `~/.ai-orchestrator/worktrees/…`. Khi xong, orchestrator commit các thay đổi vào nhánh đó (bỏ qua `__pycache__`, `node_modules`, `.venv`…). Trên dashboard bạn có thể:
- **Xem diff**
- **Merge vào nhánh gốc**: chỉ chạy khi checkout của bạn đang ở đúng nhánh và không có thay đổi chưa commit. Nếu conflict, orchestrator huỷ merge và không đổi gì.
- **Xoá worktree**

Worktree được tạo từ commit HEAD. Riêng `CLAUDE.md` có thay đổi chưa commit (ví dụ vừa được Reflection Coach cập nhật) sẽ được chép vào worktree để agent đọc được ngay, và không bị commit vào nhánh task. Hãy commit nó khi bạn đã hài lòng.

Nếu dự án cần cài dependency trước khi test được, đặt `workspace.setup_cmd` (vd `npm ci`). Nếu workdir không phải git repo, task chạy ngay trong thư mục đó, và mỗi thư mục chỉ chạy một task một lúc.

**Khi có sự cố**

| Sự cố | Orchestrator làm gì |
|---|---|
| Server quá tải (529/503), lỗi 429 ngắn, lỗi mạng | Chờ 30s, 60s, 120s… rồi thử lại cùng task (tối đa 5 lần), không đổi tài khoản |
| Hết quota ("hit your limit", "usage limit reached") | Cho tài khoản nghỉ tới giờ reset (đọc từ thông báo hoặc từ số liệu usage), chuyển session sang tài khoản khác và làm tiếp |
| Tài khoản bị đăng xuất / token hết hạn | Tạm gác tài khoản, báo bạn chạy `python -m orchestrator login <tên>` (lệnh này tự gỡ trạng thái tạm gác), task chuyển sang tài khoản khác |
| Session không resume được | Bỏ session đó và chạy lại task từ đầu, kèm tin nhắn của bạn |

Chỉ những thông báo lỗi ngắn do CLI in ra mới được phân loại, để một task *về* rate limiting có thất bại cũng không làm tài khoản bị khoá nhầm.

## Cấu hình quan trọng

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `claude.permission_mode` | `auto` | `auto` = không hỏi quyền, có classifier an toàn. `acceptEdits` chặt hơn. `bypassPermissions` chỉ nên dùng trong sandbox/VM. |
| `claude.switch_at_utilization` | `0.9` | Khi cửa sổ 5 giờ của tài khoản đạt mức này, ưu tiên tài khoản khác |
| `claude.handoff_on_limit` | `true` | Mang session sang tài khoản mới và `--resume` |
| `claude.allowed_tools` | `[]` | Allowlist; tự cập nhật khi bạn duyệt đề xuất quyền |
| `claude.max_turns` / `max_budget_usd` | 80 / 0 | Giới hạn an toàn cho mỗi lần chạy |
| `brain.provider` / `brain.model` | `claude` / `sonnet` | Ai phân loại task: `claude` (một lần gọi tinh gọn), `local`, hoặc `keywords` |
| `quick.enabled` / `quick.model` / `quick.max_complexity` | true / `sonnet` / 3 | Task chỉ cần văn bản được trả lời bằng một lần gọi Claude, không mở phiên agent |
| `claude.model` | "" | Model cho task khó (để trống = model mặc định của tài khoản) |
| `claude.light_model` / `light_max_complexity` | `sonnet` / 3 | Task có độ khó ≤3 chạy bằng Sonnet |
| `local.enabled` | false | Bật AI local cho task văn bản đơn giản |
| `router.local_categories` | summarize, translate… | Các loại việc được coi là "chỉ cần văn bản" (giao cho quick hoặc local) |
| `router.local_max_complexity` | 2 | Độ khó tối đa (1–5) được giao cho local |
| `router.min_local_success` | 0.6 | Nếu tỉ lệ thành công của local thấp hơn mức này, loại việc đó chuyển sang Claude |
| `notify.ntfy_url` | "" | vd `https://ntfy.sh/ten-bi-mat-cua-ban`; cài app ntfy trên điện thoại để nhận thông báo |
| `auto_accept` | false | true = task verify PASS thì tự chuyển sang Xong |
| `auth_token` | tự sinh | API luôn yêu cầu token. Để trống thì hệ thống tự sinh và lưu ở `~/.ai-orchestrator/token`; `serve` in ra link có sẵn token |
| `shared_config_dir` | `~/.claude` | Skills, agents, commands, plugins, CLAUDE.md, settings.json (hooks) và MCP servers ở đây được liên kết sang mọi tài khoản |
| `learning.auto_approve_llm_lessons` | false | Bài học do model tự rút ra phải chờ bạn duyệt |
| `learning.coach_every_days` / `coach_min_tasks` | 7 / 5 | Reflection Coach chạy mỗi 7 ngày nếu có ít nhất 5 task mới xong. `0` = chỉ chạy khi bạn bấm nút |
| `workspace.use_worktrees` | true | Mỗi task một git worktree + nhánh riêng |
| `workspace.setup_cmd` | "" | Lệnh chạy một lần trong mỗi worktree mới, vd `npm ci` |
| `claude.transient_retries` | 5 | Số lần thử lại khi nhà cung cấp quá tải |

## Xem dashboard từ điện thoại

Dashboard mặc định chỉ nghe ở `127.0.0.1`. Để xem từ điện thoại:

1. Lấy token trong link mà `serve` in ra (hoặc trong file `~/.ai-orchestrator/token`).
2. Cài [Tailscale](https://tailscale.com) trên cả máy tính và điện thoại, đặt `"host": "0.0.0.0"`.
3. Mở `http://<tên-máy-tailscale>:8787/?token=...`.

Đừng mở port này ra Internet công cộng: ai vào được dashboard là giao được việc cho AI chạy trên máy bạn.

## Tự học hoạt động thế nào

1. **Reflection Coach** (tab Brain), ý tưởng lấy từ [Paperclip](https://github.com/paperclipai/paperclip):
   - Mỗi 7 ngày (hoặc khi bạn bấm "Chạy reflection ngay"), Claude đọc lịch sử các task gần đây: prompt, kết quả, lệnh verify thất bại, lỗi tool, nhận xét khi bạn từ chối.
   - Nó tìm **lỗi lặp lại** và đề xuất thay đổi nhỏ, bền vững: thêm một mục vào CLAUDE.md (toàn cục hoặc của dự án), hoặc tạo/sửa một skill.
   - Mỗi đề xuất phải dẫn **task làm bằng chứng**; không có bằng chứng thì bị loại.
   - Đề xuất phải nhỏ: CLAUDE.md tăng tối đa 20%, skill tối đa 15KB.
   - Coach chạy với tool **chỉ đọc** và không ghi gì cả. Chỉ khi bạn bấm **Duyệt & áp dụng**, orchestrator mới ghi file (có sao lưu). Nút **Hoàn tác** khôi phục bản cũ.
   - Vì skills và CLAUDE.md được dùng chung cho mọi tài khoản, cải tiến sẽ có hiệu lực ở mọi nơi.
2. **Bài học ngắn**: nhận xét khi bạn từ chối và các lệnh verify thất bại được chèn vào prompt của task sau. Bạn bật/tắt/xoá hoặc tự thêm bài học trong tab Brain.
3. **Router học**: tỉ lệ thành công theo từng (loại việc, agent) quyết định lần sau giao cho ai. "Thành công" do bạn duyệt và lệnh verify quyết định.
4. **Quyền**: tool bị auto mode chặn nhiều lần sẽ thành đề xuất. Bạn duyệt một lần là nó được ghi vào `allowed_tools`.

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
