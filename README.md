# autonews

每小時自動抓台灣熱門中文話題，改寫成 Facebook 粉專圖文新聞，配上依內容生成的圖片，通過自動檢查後**立即公開**。

> 全自動發文的責任在粉專經營者身上。自動檢查只能降低風險，不能歸零；上線初期請每天看一次 `reports\` 報告。

## 這個 APP 是做什麼的

### 用途
autonews 是一個「**FB 新聞粉專的自動小編**」。它在你的電腦上每小時跑一次，替你完成一般小編每篇要花 30–60 分鐘的工作：

1. **追熱門**：從 Google Trends 台灣熱搜和多家中文新聞媒體，找出現在大家在討論什麼。
2. **選題**：在幾百個候選中，挑出最熱、最新、跟今天已發內容不重複類別的那一則。
3. **讀報導**：同一則新聞若有好幾家媒體報導，會一起讀，整合成完整的來龍去脈。
4. **寫稿**：用口語、有畫面感的方式改寫，讓讀者一看就懂發生什麼事。
5. **做圖卡**：依內容生成一張 4:5 直式圖卡，上面有大標題和一行重點。
6. **發文**：直接發佈到你的粉專。

從抓資料到發出去，一篇約 2–3 分鐘，全程不需要人操作。

### 適合誰
- 經營新聞、時事、懶人包類型 FB 粉專的個人或小團隊，想維持穩定更新頻率。
- 想研究「AI 自動產製新聞內容」流程與風險的學生、研究者。
- 想參考「多道自動檢查＋AI 生成」架構的開發者。

### 一篇貼文長什麼樣子
**文字**
- **標題**：30 字內，吸睛但不誇大。
- **正文**：450–800 字，分 4–7 段，每段 2–4 句，前後連貫。人名、數字、日期只用報導裡有的，不可編造。
- **【小編觀點】**：最後一段，寫感想或幽默收尾。不可出現數字、人名、指控用語，也不可支持或攻擊特定政黨。
- **AI 生成聲明**：「（示意圖／AI生成，僅作為新聞說明輔助使用）」。
- **hashtag**：2–5 個。

**圖卡**
- 以公眾人物為主角的新聞（例如企業負責人、藝人、政治人物）：該人物的寫實人像，只畫人物本身，不畫事件情境。
- 抽象主題：用插畫或圖表幫助理解。例如股市用紅漲綠跌的走勢圖，奇聞用可愛插畫。
- 圖上有大標題，加一行從正文摘出的重點句。

### 會選什麼題目、不會選什麼
- **類別輪替**：政治、財經、國際、社會、娛樂、體育、生活科技、奇聞，8 類輪流，避免整天都是同一類。
- **奇聞軼事**：舊聞也可以發，不限新舊。
- **一律不發**：自殺、性犯罪被害人、兒少相關題材（`config\blocklist.txt`）。
- **只用可信媒體當來源**：白名單在 `config\config.toml` 的 `[media.publishers]`。轉載平台（Yahoo、LINE TODAY）不算來源。

### 它不做的事
- 不回覆留言、不私訊、不分析粉專互動數據。
- 不保證內容百分之百正確。AI 事實審稿預設關閉以求速度，只保留程式閘門；需要更嚴謹時可以改設定開回來。
- 不抓付費牆或需要登入的內容。

### 使用前請了解的風險
- **事實錯誤**：AI 改寫可能出錯。程式會擋掉來源沒有的數字，但無法擋掉所有錯誤。
- **原創性**：稿件是改寫，與原文連續重疊不可超過 60 字。但 Meta 仍可能把大量轉述新聞判為非原創內容，影響觸及。
- **寫實人像**：AI 生成的真人寫實圖，截圖轉傳後可能被誤認為真照片。程式只畫人物本身、不畫事件，貼文也有 AI 生成聲明，但風險無法歸零。
- **額度與成本**：每小時一篇會持續消耗 Claude 與 ChatGPT 的訂閱額度，見下方「成本」。

## 運作流程

1. **收集**：Google Trends 台灣熱搜＋中文新聞 RSS（自由時報、ETtoday、公視、鏡週刊、中央社，含奇聞／寵物專區）。
2. **排序**：每個候選題目算 0–100 分（熱度 50／新鮮 25／類別輪替 10／媒體輪替 5／好寫 10），同一則新聞的多家報導合併。
3. **篩選**：黑名單、可信媒體、時效、來源數。每輪最多看 10 個候選。
4. **寫稿**：Claude 整合多家來源，寫成通順的貼文。
5. **檢查**：程式閘門檢查 hashtag、相對時間詞、易變數據、原創重疊、【小編觀點】段。
6. **配圖**：
   - AI 依內容決定配圖：公眾人物新聞畫寫實人像，抽象主題畫插畫或圖表。
   - 圖中放標題與一行重點。
   - Claude 看圖比對字有沒有錯；被拒或字錯時，改用無字圖再由程式疊標題。
7. **發佈**：用 Graph API 立即公開到粉專。

## 系統需求

| 項目 | 版本／說明 |
|---|---|
| 作業系統 | Windows 10/11（指令都是 PowerShell） |
| Python | 3.12（實測 3.12.10） |
| Codex CLI | 0.166.0 以上（舊版可能所有模型都被拒，先 `codex update`） |
| Claude Code CLI | 2.1.319 以上（需支援 `--safe-mode`） |
| ChatGPT 帳號 | 給 Codex 生圖用，用 ChatGPT 帳號登入即可 |
| Claude 訂閱 | 給寫稿、配圖規格、看圖比對用，用訂閱帳號登入 |
| Facebook 粉專 | 需要粉專管理權限，用來取得粉專 token |

## 安裝

### 1. 下載專案並建立 Python 環境
```powershell
git clone https://github.com/ldw80203/autonews.git
Set-Location autonews
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```
之後所有指令都用 `.\.venv\Scripts\python.exe`。系統內建的 python 沒有裝這些套件。

### 2. 登入 AI 帳號（預設：ChatGPT 帳號登入，不用 API 金鑰）

**Codex（生圖）預設用 ChatGPT 帳號登入**，不需要 OpenAI API key：
```powershell
codex login
codex login status    # 應顯示 Logged in using ChatGPT
```
如果你想改用 OpenAI API key（依用量另外計費），才用 `codex login --with-api-key`。

**Claude（寫稿、配圖規格、看圖比對）用訂閱帳號登入**：
```powershell
claude auth login
claude auth status
```

> **不要設定 `ANTHROPIC_API_KEY` 環境變數給本專案用。** 管線會刻意不把它傳給 claude，強制走訂閱登入。
> 原因：工作排程器會讀到使用者層級的環境變數；如果 API 餘額不足，claude 會一直回 `Credit balance is too low`。

### 3. 設定 Facebook 粉專 token
```powershell
Copy-Item .env.example .env
notepad .env
```
填入：
```
FB_PAGE_ID=你的粉專 ID
FB_PAGE_TOKEN=粉專 token（不是個人 token）
FB_GRAPH_VERSION=v27.0
```
- token 需要權限：`pages_manage_posts`、`pages_read_engagement`、`pages_show_list`。
- 長期使用請換成不過期的粉專 token。
- `.env` 已列入 `.gitignore`，不會進 git。
- 驗證連線：會發一則「未公開」的測試圖，再立刻刪除。
  ```powershell
  .\.venv\Scripts\python.exe -m src.publish --test <任一張 png 的路徑>
  ```

### 4. 先試跑（不會發文）
```powershell
.\.venv\Scripts\python.exe run_daily.py --dry-run --max 1
.\.venv\Scripts\python.exe -m pytest -q
```
看 `reports\<日期>.md` 和 `posts\<日期>\run-*\` 底下的稿件與圖片，確認沒問題再上線。

### 5. 上線：安裝每小時排程
```powershell
.\scripts\install_schedule.ps1
schtasks /Query /TN autonews-hourly /V /FO LIST
```
- 每天 00:00 起每小時整點跑一次 `run_daily.py --live --max 1`，產出後立即公開。
- 每次最多跑 55 分鐘，同時只會跑一份。
- 要登入 Windows 才會跑，執行時會跳出主控台視窗，**不要關掉它**，關掉就中斷。
- 移除排程：`schtasks /Delete /TN autonews-hourly /F`

## 常用指令

```powershell
.\.venv\Scripts\python.exe run_daily.py --live --max 1          # 手動跑一篇並立即公開
.\.venv\Scripts\python.exe run_daily.py --stage select          # 只看選題與來源關診斷，不寫稿
.\.venv\Scripts\python.exe scripts\publish_now.py <DB id>       # 把 DB 裡已排程的貼文改成立即公開
.\.venv\Scripts\python.exe scripts\manual_now.py <專案根> <post 目錄> [--post]   # 手動發一篇寫好的稿
```
- 報告：`reports\<日期>.md`（發了什麼、丟了什麼、原因）
- 執行紀錄：`logs\run.log`
- 每篇的稿件、來源、圖片、配圖規格：`posts\<日期>\run-<時分秒>-<pid>\<題目>\`

## 緊急停止
```powershell
New-Item .\STOP -ItemType File     # 停止：之後的執行直接退出，執行中的會在下一個檢查點停下
Remove-Item .\STOP                 # 恢復
```
已經公開的貼文要到 Meta Business Suite 手動刪除。

## 主要設定（`config\config.toml`）

| 設定 | 預設 | 說明 |
|---|---|---|
| `[schedule] publish_now` | `true` | `true` 立即公開；`false` 排到下一個整點 |
| `[schedule] slots` | 00:00–23:00 | `publish_now = false` 時使用的時段 |
| `[review] ai_review` | `false` | AI 事實審稿（Codex＋Claude）；關閉時只跑程式閘門 |
| `[image] content_image` | `true` | 依內容生成配圖；`false` 改用固定場景模板加程式疊字 |
| `[rank] weights` | 熱度 50／新鮮 25／類別 10／媒體 5／好寫 10 | 選文權重 |
| `[freshness] max_age_hours` | 18 | 來源超過幾小時算過期 |
| `[freshness] unlimited_categories` | `["奇聞"]` | 這些類別不限新舊 |
| `[select] max_candidates_fetch` | 10 | 每輪最多看幾個候選 |
| `[run] deadline_minutes` | 55 | 每輪時間上限（排程時限讀同一個值） |
| `[run] max_rewrites` | 1 | 稿子被退回後重寫幾次 |
| `[run] max_claude_failures` | 3 | claude 一輪失敗幾次就停（多半是額度用完） |
| `config\blocklist.txt` | 自殺、性犯罪被害人、兒少 | 命中就不發 |

## 成本（實測，2026-11-09）

一篇貼文約用：
- **Claude**：3 次呼叫，約 1.9 萬輸入 token、2.7 千輸出 token，API 等值約 USD 0.08（約 NT$2.6）。
- **Codex 生圖**：約 3.4 萬 token 加 1 張圖。API 等值價格未確認，參考約 NT$4.4。

用訂閱登入時不會另外扣款，消耗的是訂閱額度。每小時一篇時，請留意 Claude 的 5 小時額度。

## 疑難排解

| 狀況 | 原因／處理 |
|---|---|
| log 出現 `claude 失敗 ... Credit balance is too low` | claude 讀到了 API key；確認版本有排除 `ANTHROPIC_API_KEY`，或執行 `claude auth status` |
| log 出現 `claude 已失敗 3 次，停止本輪` | 多半是 Claude 訂閱額度用完，等額度重置後下一個整點會自動再試 |
| `image_spec.json` 出現 `codex：我不能製作…` | Codex 拒絕把涉及真人爭議的標題做成圖，程式會自動改用無字圖加疊字，不用處理 |
| 一整小時沒發文 | 10 個候選都沒過關。看 `reports\<日期>.md` 的丟棄原因 |
| pytest 有 4 個 `StopRequested` 失敗 | 專案根目錄有 `STOP` 檔，先暫時改名再跑測試 |

## 安全設計
- FB 憑證只在 `src\publish.py` 局部讀取，放在 Authorization header，不寫入環境變數。
- claude／codex 子程序只拿得到環境變數允許清單，不含 `FB_*` 和 `ANTHROPIC_API_KEY`。
- claude 用 `--safe-mode` 執行，不載入使用者的 CLAUDE.md／skills／MCP，也省 token。
- 配圖描述只准英文字母、數字和基本標點，擋檔案、指令、網址字眼。圖中中文只能是程式原樣放入的標題，以及正文裡一字不差的重點句。
- Codex 生圖在隔離的暫存目錄執行。
