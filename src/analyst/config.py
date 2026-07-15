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
    # 其它免费 OpenAI 兼容层（盯盘 free_only 与「先免费后付费」共用；有 key 才启用）
    # 申请：https://cloud.cerebras.ai → API keys
    cerebras_api_key: str = Field(default="")
    cerebras_model: str = Field(default="gpt-oss-120b")
    cerebras_base_url: str = Field(default="https://api.cerebras.ai/v1")
    # 申请：https://aistudio.google.com/apikey
    gemini_api_key: str = Field(default="")
    gemini_model: str = Field(default="gemini-flash-latest")
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    # 申请：https://openrouter.ai/keys （选 :free 模型）
    openrouter_api_key: str = Field(default="")
    openrouter_model: str = Field(default="openrouter/free")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1")
    # 申请：https://cloud.sambanova.ai/apis
    sambanova_api_key: str = Field(default="")
    sambanova_model: str = Field(default="Meta-Llama-3.3-70B-Instruct")
    sambanova_base_url: str = Field(default="https://api.sambanova.ai/v1")
    # 申请：https://build.nvidia.com → API Key
    nvidia_api_key: str = Field(default="")
    nvidia_model: str = Field(default="deepseek-ai/deepseek-v4-flash")
    nvidia_base_url: str = Field(default="https://integrate.api.nvidia.com/v1")
    # 免费层尝试顺序（逗号分隔）；仅配置了 key 的会实际调用
    llm_free_order: str = Field(
        default="nvidia,groq,cerebras,openrouter,sambanova,gemini"
    )
    # b.ai：免费层失败后的第二段（完整 prompt，tool 为 auto）；再失败走 LLM_PROVIDER。
    # 未配任何免费 key 时，b.ai 仍会先于主线路执行（即 b.ai → DeepSeek）。
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
    monitor_stop_buffer_atr_mult: float = Field(default=1.0)  # 缓冲被 ATR 封顶；0=禁用
    monitor_take_profit_r: float = Field(default=2.0)
    monitor_max_chase_atr: float = Field(default=1.5)  # 超出突破位该倍 ATR 不追；0=禁用
    monitor_ema_trend_period: int = Field(default=200)
    monitor_require_ema200: bool = Field(default=True)
    monitor_require_ema_slope: bool = Field(default=False)  # EMA200 需同向倾斜（可选）
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
    monitor_volume_spike_ratio: float = Field(default=2.0)   # 放量告警阈值（×20 均量）
    monitor_touch_cooldown_bars: int = Field(default=12)     # 同一支撑/阻力冷却根数
    # 牛熊周期切换（方案 D）：4h 收盘评估仓位变化并推 TG
    monitor_cycle_switch_enabled: bool = Field(default=True)
    monitor_cycle_switch_timeframe: str = Field(default="4h")
    # cycle_switch 跟单/告警白名单；空=全部盯盘品种。默认砍弱 beta（如 AAVE）
    monitor_cycle_symbols: str = Field(default="BTC/USDT,ETH/USDT,SOL/USDT")
    monitor_cycle_outlook_enabled: bool = Field(default=True)  # Wolfy 日历+狼波提醒
    # 收盘有双线/规则候选时才调 AI；long/short → 盯盘点评通知（不开纸面）
    monitor_ai_on_candidate: bool = Field(default=True)
    monitor_ai_cooldown_minutes: int = Field(default=240)
    # 盯盘 AI 确认只走免费层（Groq/Cerebras/Gemini/OpenRouter/SambaNova）；失败不回落付费
    monitor_ai_free_only: bool = Field(default=True)
    # 纸面模拟炒币：只跟规则策略，不跟 AI
    monitor_paper_enabled: bool = Field(default=True)
    monitor_paper_equity: float = Field(default=100.0)
    monitor_paper_risk_pct: float = Field(default=0.01)
    monitor_paper_fee_bps: float = Field(default=4.0)
    # 展示与保证金占用：保证金 = 名义 / 杠杆；收益率 = 浮盈 / 保证金
    monitor_paper_leverage: float = Field(default=5.0)
    monitor_paper_max_positions: int = Field(default=12)
    monitor_paper_tg: bool = Field(default=True)
    # 纸面跟单来源：double_line,cycle_switch（不含 ai_plan）
    monitor_paper_sources: str = Field(
        default="double_line,cycle_switch"
    )
    # Telegram 白名单（页面仍可看到全部规则告警）。空=全部推 TG（旧行为）
    # 默认：AI 点评 + 异动类规则（金叉死叉/放量/突破等）；cycle 仓位变化仍不直推
    monitor_tg_trade_rules: str = Field(
        default="ai_plan,macd_cross,ema_stack,volume,boll_break,break_level,funding_extreme"
    )
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
    def cycle_symbols_set(self) -> set[str] | None:
        """cycle_switch 白名单。None=不限制；否则仅集合内品种评估/纸面跟单。"""
        raw = (self.monitor_cycle_symbols or "").strip()
        if not raw:
            return None
        out: set[str] = set()
        for s in raw.split(","):
            s = s.strip().upper().replace("-", "/")
            if not s:
                continue
            if "/" not in s:
                if s.endswith("USDT") and len(s) > 4:
                    s = f"{s[:-4]}/USDT"
                else:
                    s = f"{s}/USDT"
            out.add(s.split(":")[0])
        return out or None

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
    def tg_trade_rules_set(self) -> set[str] | None:
        """None=不限制（全部规则可推 TG）；否则仅集合内规则推 TG。"""
        raw = (self.monitor_tg_trade_rules or "").strip()
        if not raw:
            return None
        return {x.strip().lower() for x in raw.split(",") if x.strip()}

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
