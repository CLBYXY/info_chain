"""运行配置：全部来自环境变量，便于本地与部署环境切换。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 本地 .env（已在 .gitignore 中）：只在环境变量未设置时生效
_env = ROOT / ".env"
if _env.exists():
    for _line in _env.read_text("utf-8").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

DATA_DIR = Path(os.getenv("EVENTLENS_DATA_DIR", ROOT / "data"))
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 扶摇（同花顺金融数据）MCP
FUYAO_BASE = os.getenv("FUYAO_BASE_URL", "https://fuyao.aicubes.cn/mcp")
FUYAO_KEY = os.getenv("HITHINK_FINANCE_API_KEY", "")

# LLM：deepseek（默认，OpenAI 兼容接口）或 anthropic。
# 未配置对应凭证时，所有 LLM 环节按设计文档的“保守降级”规则运行。
LLM_PROVIDER = os.getenv("EVENTLENS_LLM_PROVIDER", "deepseek")
_DEFAULT_MODEL = {"deepseek": "deepseek-flash", "anthropic": "claude-opus-5-5"}
LLM_MODEL = os.getenv("EVENTLENS_MODEL", _DEFAULT_MODEL.get(LLM_PROVIDER, ""))
DEEPSEEK_BASE = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
_HAS_KEY = {
    "deepseek": bool(os.getenv("DEEPSEEK_API_KEY")),
    "anthropic": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")),
}
LLM_ENABLED = os.getenv("EVENTLENS_LLM", "auto") != "off" and _HAS_KEY.get(LLM_PROVIDER, False)

HTTP_TIMEOUT = float(os.getenv("EVENTLENS_HTTP_TIMEOUT", "20"))
HTTP_RETRIES = 2
DEFAULT_WINDOW_DAYS = 90
TZ = "Asia/Shanghai"
