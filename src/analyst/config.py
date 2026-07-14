"""配置管理 - 从环境变量加载，类型安全。"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。所有变量在 .env 中定义。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM Provider
    # 可选: 'openai' (含 Groq/OpenRouter/b.ai 等 OpenAI 兼容) / 'deepseek' / 'anthropic'
    llm_provider: str = Field(default="deepseek")
    llm_model: str = Field(default="deepseek-v4-flash")
    llm_base_url: str = Field(default="https://api.deepseek.com")
    llm_temperature: float = Field(default=0.3)
    llm_max_tokens: int = Field(default=4000)
    llm_prompt_version: str = Field(default="v1")

    # Provider 各自的 API Key（按需填）
    deepseek_api_key: str = Field(default="")
    anthropic_api_key: str = Field(default="")
    openai_api_key: str = Field(default="")
    # Groq：若配置 GROQ_API_KEY，则分析时默认先试 Groq（压缩 prompt），失败再回退 LLM_PROVIDER
    groq_api_key: str = Field(default="")
    groq_model: str = Field(default="llama-3.3-70b-versatile")
    groq_base_url: str = Field(default="https://api.groq.com/openai/v1")
    groq_max_tokens: int = Field(default=4096)
    llm_try_groq_first: bool = Field(default=True)
    # b.ai：Groq 失败后的第二段（完整 prompt，tool 为 auto）；再失败走 LLM_PROVIDER。
    # 未配 GROQ_API_KEY 时，b.ai 仍会先于主线路执行（即 b.ai → DeepSeek）。
    bai_api_key: str = Field(default="")
    bai_base_url: str = Field(default="https://api.b.ai/v1")
    bai_model: str = Field(default="")
    llm_try_bai_after_groq: bool = Field(default=True)

    # DeepSeek V4：OpenAI SDK 文档中的 thinking / reasoning_effort
    # https://api-docs.deepseek.com/zh-cn/ — reasoning_effort 留空则省略
    deepseek_reasoning_effort: str = Field(default="high")
    deepseek_thinking_enabled: bool = Field(default=True)

    # 数据源
    exchange: str = Field(default="binance")
    default_symbols: str = Field(
        default="BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,SUI/USDT,UNI/USDT"
    )
    # 遗留项（当前 Web 已固定 U 本位，代码未再读取）；保留以免旧 .env 报未知字段
    futures_only_symbols: str = Field(default="")

    data_cache_dir: str = Field(default=".cache/data")
    data_cache_ttl_minutes: int = Field(default=5)

    # 会话与分析默认参数
    default_timeframe: str = Field(default="4h")
    verification_delay_hours: int = Field(default=72)
    session_expire_hours: int = Field(default=168)

    # 风控
    max_risk_per_trade_pct: float = Field(default=1.0)
    max_leverage: int = Field(default=10)
    default_account_usd: float = Field(default=10000)

    # 实时监控（双线反转 K 线形态 + Binance WS）
    monitor_market: str = Field(default="futures")  # 监控页固定 U 本位合约
    monitor_timeframe: str = Field(default="15m")
    monitor_kelly_scale: float = Field(default=0.25)
    monitor_stop_buffer_pct: float = Field(default=2.0)
    monitor_take_profit_r: float = Field(default=2.0)
    monitor_ema_trend_period: int = Field(default=200)
    monitor_require_ema200: bool = Field(default=True)
    monitor_trail_to_8r: bool = Field(default=False)
    monitor_require_fib_zone: bool = Field(default=False)
    monitor_require_volume: bool = Field(default=False)
    # 规则引擎（无 AI 实时提醒，默认全开）
    monitor_rules_enabled: bool = Field(default=True)
    monitor_rule_macd: bool = Field(default=True)
    monitor_rule_ema_stack: bool = Field(default=True)
    monitor_rule_boll: bool = Field(default=True)
    monitor_rule_volume: bool = Field(default=True)
    monitor_rule_structure_touch: bool = Field(default=True)
    monitor_rule_structure_flip: bool = Field(default=True)
    monitor_rule_fib_zone: bool = Field(default=True)
    monitor_rule_baseline: bool = Field(default=True)
    monitor_rule_break_level: bool = Field(default=True)
    monitor_rule_funding: bool = Field(default=True)
    monitor_rule_premium: bool = Field(default=True)
    monitor_funding_extreme_pct: float = Field(default=0.05)
    monitor_premium_extreme_pct: float = Field(default=0.30)
    # 关网页也继续盯盘 + Telegram（Web 进程需保持运行）
    monitor_always_on: bool = Field(default=False)
    # 常驻盯盘品种；空则用 DEFAULT_SYMBOLS
    monitor_daemon_symbols: str = Field(default="")
    # 常驻多级别周期（逗号分隔）；空则仅 MONITOR_TIMEFRAME
    monitor_daemon_timeframes: str = Field(default="15m,1h,4h")
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # 数据库
    database_url: str = Field(default="sqlite:///./analyst.db")

    # 日志
    log_level: str = Field(default="INFO")
    log_file: str = Field(default=".logs/analyst.log")

    @property
    def symbols_list(self) -> list[str]:
        return [s.strip() for s in self.default_symbols.split(",") if s.strip()]

    @property
    def daemon_symbols_list(self) -> list[str]:
        raw = (self.monitor_daemon_symbols or "").strip()
        if raw:
            return [s.strip() for s in raw.split(",") if s.strip()]
        return self.symbols_list

    @property
    def daemon_timeframes_list(self) -> list[str]:
        raw = (self.monitor_daemon_timeframes or "").strip()
        if raw:
            tfs = [t.strip().lower() for t in raw.split(",") if t.strip()]
            # 去重保序
            seen: set[str] = set()
            out: list[str] = []
            for t in tfs:
                if t not in seen:
                    seen.add(t)
                    out.append(t)
            if out:
                return out
        tf = (self.monitor_timeframe or "15m").strip().lower()
        return [tf] if tf else ["15m"]

    @property
    def futures_only_list(self) -> list[str]:
        return [s.strip().upper() for s in self.futures_only_symbols.split(",") if s.strip()]

    @property
    def cache_path(self) -> Path:
        return Path(self.data_cache_dir)


_settings: Settings | None = None


def get_settings() -> Settings:
    """单例。"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
