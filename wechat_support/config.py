"""Settings: config/assistant.yaml for the business, the environment (or the macOS Keychain)
for anything secret. Secrets are only read when needed and are never logged."""
import os
import subprocess
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent

# secret name -> default Keychain item (account "neo" unless KEYCHAIN_ACCOUNT says otherwise).
# Each can be overridden with <NAME>_KEYCHAIN, or given directly as the env var <NAME>.
SECRET_KEYCHAIN_ITEMS = {
    "WECHAT_APPSECRET": "wechat-test-appsecret",
    "WECHAT_TOKEN": "wechat-callback-token",
    "WECHAT_AES_KEY": "wechat-encoding-aes-key",
    "ANTHROPIC_API_KEY": "anthropic-api-key",
    "GEMINI_API_KEY": "gemini-api-key",
    "ADMIN_TOKEN": "wechat-admin-token",
}


def keychain_lookup(item: str, account: Optional[str] = None) -> Optional[str]:
    account = account or os.environ.get("KEYCHAIN_ACCOUNT", "neo")
    for args in (["-a", account, "-s", item], ["-s", item]):
        try:
            out = subprocess.run(["security", "find-generic-password", *args, "-w"],
                                 capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        value = out.stdout.strip()
        if out.returncode == 0 and value:
            return value
    return None


class Secrets:
    """Env var first, then the Keychain. Values are cached in memory only."""

    def __init__(self, env=None, keychain: Optional[Callable[[str], Optional[str]]] = None):
        self.env = os.environ if env is None else env
        self.keychain = keychain_lookup if keychain is None else keychain
        self._cache: Dict[str, Optional[str]] = {}

    def get(self, name: str) -> Optional[str]:
        if name in self._cache:
            return self._cache[name]
        value = self.env.get(name) or None
        if value is None:
            item = self.env.get(f"{name}_KEYCHAIN") or SECRET_KEYCHAIN_ITEMS.get(name)
            if item and self.env.get("NO_KEYCHAIN") != "1":
                value = self.keychain(item)
        self._cache[name] = value
        return value

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            item = SECRET_KEYCHAIN_ITEMS.get(name, name.lower())
            raise RuntimeError(f"{name} is not set (env var {name}, or Keychain item '{item}')")
        return value


@dataclass
class AISettings:
    provider: str = "anthropic"           # anthropic | gemini | scripted
    model: str = "claude-sonnet-5-5"
    gemini_model: str = "gemini-3.5-flash-lite"
    max_sections: int = 4
    max_tokens: int = 700
    max_words: int = 90
    max_chars: int = 180
    per_user_daily: int = 20
    daily: int = 500
    scripted_delay: float = 0.0


@dataclass
class WeChatSettings:
    appid: str = ""
    api_base: str = "https://api.weixin.qq.com"
    token_mode: str = "stable"            # stable | classic
    passive_budget_seconds: float = 4.0   # WeChat waits 5 s; answer passively only if ready by then
    cs_window_hours: float = 48
    cs_per_window: int = 5                # customer-service messages allowed since the follower's last message
    timestamp_max_age: int = 300
    require_encryption: bool = False      # refuse plaintext callbacks (set once safe mode is on)
    typing_indicator: bool = True


@dataclass
class HandoverSettings:
    keywords: List[str] = field(default_factory=lambda: ["人工", "转人工", "人工客服", "真人", "human", "agent",
                                                         "real person", "talk to a person", "speak to someone"])
    mode: str = "notify"                  # notify | transfer_customer_service
    webhook_url: str = ""                 # optional: POSTed a JSON notice for the team
    on_not_found: bool = False            # also hand over when the knowledge base has no answer


@dataclass
class Settings:
    brand: str = "Lumen Tea House"
    database: str = "data/assistant.db"
    knowledge_base: str = "knowledge_base"
    default_lang: str = "en"              # for messages with no letters at all (emoji, numbers)
    ai: AISettings = field(default_factory=AISettings)
    wechat: WeChatSettings = field(default_factory=WeChatSettings)
    handover: HandoverSettings = field(default_factory=HandoverSettings)
    welcome: Dict[str, str] = field(default_factory=lambda: {
        "en": "Thanks for following! Ask me anything about orders, delivery or our teas. Type \"human\" for a person.",
        "zh": "感谢关注！关于订单、配送或茶品的问题都可以问我。需要人工服务请回复“人工”。"})
    unsupported: Dict[str, str] = field(default_factory=lambda: {
        "en": "Sorry, I can only read text messages. Please type your question.",
        "zh": "抱歉，我目前只能看文字消息，请直接输入您的问题。"})
    holding: Dict[str, str] = field(default_factory=lambda: {
        "en": "A colleague has been told and will reply here soon.",
        "zh": "已通知同事，稍后会在这里回复您。"})
    busy: Dict[str, str] = field(default_factory=lambda: {
        "en": "Sorry, I can't answer more questions today. A colleague will reply to you.",
        "zh": "抱歉，今天的自动回复已达上限，同事会尽快回复您。"})
    error: Dict[str, str] = field(default_factory=lambda: {
        "en": "Sorry, something went wrong on our side. A colleague will reply to you.",
        "zh": "抱歉，系统出了点问题，同事会尽快回复您。"})


def _merge(obj, data: dict):
    names = {f.name for f in fields(obj)}
    for key, value in (data or {}).items():
        if key not in names:
            raise ValueError(f"unknown setting {key!r} in {type(obj).__name__}")
        current = getattr(obj, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(obj, key, value)
    return obj


def load(path=None, env=None) -> Settings:
    env = os.environ if env is None else env
    path = Path(path or env.get("ASSISTANT_CONFIG") or ROOT / "config" / "assistant.yaml")
    s = Settings()
    if path.exists():
        _merge(s, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    # environment overrides for the things that change per deployment
    if env.get("WECHAT_APPID"):
        s.wechat.appid = env["WECHAT_APPID"]
    if env.get("WECHAT_API_BASE"):
        s.wechat.api_base = env["WECHAT_API_BASE"]
    if env.get("AI_PROVIDER"):
        s.ai.provider = env["AI_PROVIDER"]
    if env.get("ASSISTANT_DB"):
        s.database = env["ASSISTANT_DB"]
    for p in ("database", "knowledge_base"):
        v = Path(getattr(s, p))
        if not v.is_absolute():
            setattr(s, p, str(ROOT / v))
    if s.handover.mode not in ("notify", "transfer_customer_service"):
        raise ValueError("handover.mode must be notify or transfer_customer_service")
    return s
