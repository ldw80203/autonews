"""共用：路徑、設定、子程序、JSON 擷取。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tomllib
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.toml"
BLOCKLIST_PATH = ROOT / "config" / "blocklist.txt"
DB_PATH = ROOT / "data" / "autonews.db"


def as_taipei(dt: datetime) -> datetime:
    """naive 一律視為台北時間；aware 轉成台北時間。時間比較一律先過這裡，不依賴本機時區。"""
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def load_config(path: Path | None = None) -> dict:
    with open(path or CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def norm_key(text: str) -> str:
    """關鍵字正規化：NFKC、小寫、只留文字與數字。"""
    t = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\W_]+", "", t)


def slugify(text: str, maxlen: int = 24) -> str:
    base = norm_key(text)[:maxlen] or "topic"
    h = hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:6]
    return f"{base}-{h}"


_ENV_ALLOW = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "USERPROFILE", "HOME", "HOMEDRIVE",
    "HOMEPATH", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "USERNAME", "USERDOMAIN", "COMPUTERNAME",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "LANG", "LC_ALL", "PYTHONIOENCODING",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
}
_ENV_ALLOW_PREFIX = ("ANTHROPIC_", "CLAUDE", "CODEX_", "OPENAI_")
# 使用者層級環境變數有 ANTHROPIC_API_KEY（API 餘額不足）；工作排程器會帶到它，claude 就改走 API 計費而失敗
# （"Credit balance is too low"）。管線一律走訂閱 OAuth，所以不傳。2026-11-09 實測。
_ENV_DENY = {"ANTHROPIC_API_KEY"}


def safe_env(base: dict | None = None) -> dict:
    """子程序環境變數允許清單；絕不帶 FB_* 或其他未列名變數。"""
    src = os.environ if base is None else base
    out = {}
    for k, v in src.items():
        ku = k.upper()
        if ku.startswith("FB_") or ku in _ENV_DENY:
            continue
        if ku in _ENV_ALLOW or ku.startswith(_ENV_ALLOW_PREFIX):
            out[k] = v
    return out


_SECRET_PATTERNS = [
    re.compile(r"EAA[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)(access_token=|bearer\s+)[^\s&\"']+"),
]


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """遮蔽訊息中的憑證（格式比對 + 已知值）。"""
    t = str(text)
    for sec in secrets:
        if sec and len(sec) >= 6:
            t = t.replace(sec, "[REDACTED]")
    t = _SECRET_PATTERNS[0].sub("[REDACTED]", t)
    t = _SECRET_PATTERNS[1].sub(lambda m: m.group(1) + "[REDACTED]", t)
    return t


def cjk_ratio(text: str) -> float:
    """CJK 字數 / 所有字母數字與 CJK 字數。空字串回 0。"""
    cjk = len(re.findall(r"[㐀-䶿一-鿿]", text or ""))
    other = len(re.findall(r"[A-Za-z0-9À-ɏЀ-ӿ぀-ヿ가-힯]", text or ""))
    total = cjk + other
    return cjk / total if total else 0.0


def run_cmd(args: list[str], timeout: int, input_text: str = "", cwd: str | Path | None = None):
    """跑子程序（UTF-8、有 timeout、env 允許清單）。回 (returncode, stdout, stderr)；逾時回 (-9, '', 'timeout')。"""
    try:
        p = subprocess.run(
            args, input=input_text, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=str(cwd) if cwd else None, env=safe_env(),
        )
        return p.returncode, p.stdout or "", redact(p.stderr or "")
    except subprocess.TimeoutExpired:
        return -9, "", f"timeout after {timeout}s"
    except OSError as e:
        return -1, "", f"OSError: {redact(e)}"


CLAUDE_SYSTEM_PROMPT = "Follow the user's instructions exactly. Output only what they ask for."


def claude_error(stdout: str) -> str:
    """claude --output-format json 失敗時，stdout 的 result 欄位有原因（例：Credit balance is too low）。"""
    import json
    try:
        j = json.loads(stdout or "")
    except ValueError:
        return ""
    return str(j.get("result") or "")[:200] if isinstance(j, dict) and j.get("is_error") else ""


def claude_args(exe: str, model: str) -> list[str]:
    """`claude -p` 共用旗標。--safe-mode 不載入使用者的 CLAUDE.md/skills/plugins/hooks/MCP、仍吃 OAuth
    （2026-11-09 實測 sonnet 小呼叫：input 約 16 萬 → 2.5k token）。--bare 只吃 API key，不能用。"""
    return [exe, "-p", "--output-format", "json", "--model", model, "--tools", "",
            "--safe-mode", "--system-prompt", CLAUDE_SYSTEM_PROMPT, "--no-session-persistence"]


def fold_width(text: str) -> str:
    """只把全形 ASCII（！～）轉半形、全形空白轉半形空白。刻意不用 NFKC：NFKC 會把 10² 變 102、½ 變 1⁄2。"""
    out = []
    for ch in text or "":
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif o == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


class DuplicateKeyError(ValueError):
    pass


def _no_dup_pairs(pairs):
    """object_pairs_hook：同一物件內出現重複鍵就拒絕（{"pass":false,"pass":true} 不可解析成 pass=true）。"""
    out = {}
    for k, v in pairs:
        if k in out:
            raise DuplicateKeyError(f"重複鍵 {k!r}")
        out[k] = v
    return out


def strict_loads(text: str):
    """json.loads + 拒絕任何層級的重複鍵。失敗丟 ValueError（JSONDecodeError 也是 ValueError）。"""
    return json.loads(text, object_pairs_hook=_no_dup_pairs)


def parse_single_json(text: str):
    """整段文字必須剛好是一個 JSON 物件（可被 ``` 圍欄包住）、且沒有重複鍵；否則回 None。"""
    if not text:
        return None
    t = text.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, flags=re.S)
    if m:
        t = m.group(1)
    try:
        obj = strict_loads(t)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def extract_json(text: str):
    """從文字抽出第一個完整 JSON 物件（容許 ``` 圍欄與前後雜訊）。失敗回 None。"""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return strict_loads(text[start:i + 1])
                    except DuplicateKeyError:
                        return None  # 有重複鍵：整份輸出不可信，不再往後找
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
