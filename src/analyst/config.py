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
    # b.ai：最先尝试（限时无限免费 DeepSeek-V4-Flash）；失败再走免费层 → LLM_PROVIDER。
    # LLM_TRY_BAI_AFTER_GROQ=false 可关掉这一跳。
    bai_api_key: str = Field(default="")
    bai_base_url: str = Field(default="https://api.b.ai/v1")
    bai_model: str = Field(default="deepseek-v4-flash")
    llm_try_bai_after_groq: bool = Field(default=True)

    # DeepSeek V4：OpenAI SDK 文档中的 thinking / reasoning_effort
    # https://api-docs.deepseek.com/zh-cn/ — reasoning_effort 留空则省略
    deepseek_reasoning_effort: str = Field(default="high")
    deepseek_thinking_enabled: bool = Field(default=True)

    # 数据源
    exchange: str = Field(default="binance")
    default_symbols: str = Field(
        default="BTC/USDT,ETH/USDT,BNB/USDT,UNI/USDT"
    )
    data_cache_dir: str = Field(default=".cache/data")
    data_cache_ttl_minutes: int = Field(default=5)

    # 会话与分析默认参数
    default_timeframe: str = Field(default="4h")
    verification_delay_hours: int = Field(default=72)
    session_expire_hours: int = Field(default=168)

    # 风控（仅注入 AI 分析 prompt，不下单）
    max_risk_per_trade_pct: float = Field(default=1.0)
    max_leverage: int = Field(default=10)
    default_account_usd: float = Field(default=10000)

    # 实时监控（Binance WS + 规则/周期策略）
    monitor_market: str = Field(default="futures")  # 监控页固定 U 本位合约
    monitor_timeframe: str = Field(default="15m")
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
    monitor_rule_funding: bool = Field(default=True)
    monitor_rule_premium: bool = Field(default=True)
    monitor_funding_extreme_pct: float = Field(default=0.05)
    monitor_premium_extreme_pct: float = Field(default=0.30)
    monitor_volume_spike_ratio: float = Field(default=2.0)   # 放量告警阈值（×20 均量）
    monitor_touch_cooldown_bars: int = Field(default=12)     # 同一支撑/阻力冷却根数
    # 趋势跟随规则：ADX 低于该值视为震荡，不报 MACD/EMA/布林（0=关闭）
    monitor_adx_min_trend: float = Field(default=18.0)
    # 逆更高周期 EMA 排列的趋势信号丢掉（1h 看 4h，15m 看 1h）
    monitor_htf_filter: bool = Field(default=True)
    # cycle_switch 新开仓 ADX 门槛；过低只上页面、不打 AI
    monitor_cycle_adx_min: float = Field(default=20.0)
    # AI 候选：弱规则单独命中不打；需质量规则或同根 ≥2 条
    monitor_ai_require_quality: bool = Field(default=True)
    # 牛熊周期切换（方案 D）：4h 收盘评估仓位变化并推 TG
    monitor_cycle_switch_enabled: bool = Field(default=True)
    monitor_cycle_switch_timeframe: str = Field(default="4h")
    # cycle_switch 评估/告警白名单；空=跟随盯盘品种（DEFAULT_SYMBOLS / DAEMON）
    monitor_cycle_symbols: str = Field(default="")
    monitor_cycle_outlook_enabled: bool = Field(default=True)  # Wolfy 日历+狼波提醒
    # 收盘有规则/周期候选时才调 AI；long/short → 盯盘点评（仅提醒）
    monitor_ai_on_candidate: bool = Field(default=True)
    monitor_ai_cooldown_minutes: int = Field(default=240)
    # 盯盘 AI 确认只走免费层（Groq/Cerebras/Gemini/OpenRouter/SambaNova）；失败不回落付费
    monitor_ai_free_only: bool = Field(default=True)

    # ── 横截面动量评估告警 ──
    monitor_xs_enabled: bool = Field(default=True)
    # 观察池；空 = 跟随盯盘品种（须有 4h worker）
    monitor_xs_symbols: str = Field(default="")
    monitor_xs_top_n: int = Field(default=2)
    monitor_xs_rebalance_hours: int = Field(default=168)  # 每周调仓
    monitor_xs_bear_short: bool = Field(default=True)     # 熊市空最弱

    # ── AI 日报 ──
    monitor_digest_enabled: bool = Field(default=True)
    monitor_digest_utc_hour: int = Field(default=5)  # UTC 5 点 = 迪拜早 9 点

    # ── 新闻事件风控哨兵 ──
    monitor_news_enabled: bool = Field(default=True)
    monitor_news_interval_min: int = Field(default=30)
    # 逗号分隔 RSS 源；binance = 币安公告接口
    monitor_news_feeds: str = Field(
        default=(
            "https://www.coindesk.com/arc/outboundfeeds/rss/,"
            "https://cointelegraph.com/rss,"
            "binance"
        )
    )
    # 推送门槛：high / critical
    monitor_news_min_severity: str = Field(default="high")

    # ── 汇率对（相对强弱）监控：ETH/BTC 等，只告警不交易 ──
    monitor_ratio_enabled: bool = Field(default=True)
    monitor_ratio_pairs: str = Field(default="ETH/BTC,BNB/BTC,UNI/BTC")
    monitor_ratio_ema_days: int = Field(default=200)   # 长期均线（日）
    monitor_ratio_band: float = Field(default=0.02)    # EMA 迟滞带
    monitor_ratio_break_days: int = Field(default=40)  # N 日新高/新低

    # ── 资金费套利信号评估 ──
    monitor_carry_enabled: bool = Field(default=True)
    # 空 = 跟随盯盘品种（DEFAULT_SYMBOLS / DAEMON）
    monitor_carry_symbols: str = Field(default="")
    # Telegram 白名单（页面仍可看到全部规则告警）。空=全部推 TG（旧行为）
    # 默认：AI 点评 + 异动类规则（金叉死叉/放量/突破等）；cycle 仓位变化仍不直推
    monitor_tg_trade_rules: str = Field(
        default="ai_plan,macd_cross,ema_stack,volume,boll_break,funding_extreme,ratio_shift,xs_momentum,funding_carry"
    )
    # 关网页也继续盯盘 + Telegram（Web 进程需保持运行）
    monitor_always_on: bool = Field(default=False)
    # 常驻盯盘品种；空则用 DEFAULT_SYMBOLS
    monitor_daemon_symbols: str = Field(default="")
    # 常驻多级别周期（逗号分隔）；空则仅 MONITOR_TIMEFRAME
    monitor_daemon_timeframes: str = Field(default="15m,1h,4h")
    # 盯盘图表可选周期（实时 K / WS）；默认不含 1m 等短周期
    monitor_chart_timeframes: str = Field(default="5m,15m,1h,4h")
    # 市场日程：时段 / 资金费 / 宏观日历提醒
    monitor_schedule_enabled: bool = Field(default=True)
    monitor_schedule_tg: bool = Field(default=True)
    monitor_schedule_session_leads: str = Field(default="30,15")
    monitor_schedule_funding_leads: str = Field(default="30")
    monitor_schedule_macro_leads: str = Field(default="60,30,15")
    monitor_schedule_macro_currencies: str = Field(default="USD")
    monitor_schedule_macro_impacts: str = Field(default="High")
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # 数据库
    database_url: str = Field(default="sqlite:///./analyst.db")

    # 日志
    log_level: str = Field(default="INFO")
    log_file: str = Field(default=".logs/analyst.log")

    @staticmethod
    def _csv_symbols(raw: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for s in (raw or "").split(","):
            s = s.strip().upper().replace("-", "/")
            if not s:
                continue
            if "/" not in s:
                if s.endswith("USDT") and len(s) > 4:
                    s = f"{s[:-4]}/USDT"
                else:
                    s = f"{s}/USDT"
            s = s.split(":")[0]
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    @property
    def symbols_list(self) -> list[str]:
        return self._csv_symbols(self.default_symbols)

    @property
    def cycle_symbols_set(self) -> set[str] | None:
        """cycle_switch 白名单。None=不限制（跟随盯盘品种）。"""
        parsed = self._csv_symbols(self.monitor_cycle_symbols)
        return set(parsed) or None

    @property
    def daemon_symbols_list(self) -> list[str]:
        parsed = self._csv_symbols(self.monitor_daemon_symbols)
        return parsed or self.symbols_list

    @property
    def carry_symbols_list(self) -> list[str]:
        """资金费 carry 评估品种。空则跟随盯盘品种。"""
        return self._csv_symbols(self.monitor_carry_symbols) or self.daemon_symbols_list

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
    def chart_timeframes_list(self) -> list[str]:
        """盯盘图表/WS 允许的周期。"""
        raw = (self.monitor_chart_timeframes or "").strip()
        allowed = {"5m", "15m", "1h", "4h"}
        if raw:
            tfs = [t.strip().lower() for t in raw.split(",") if t.strip()]
            seen: set[str] = set()
            out: list[str] = []
            for t in tfs:
                if t in allowed and t not in seen:
                    seen.add(t)
                    out.append(t)
            if out:
                return out
        return ["5m", "15m", "1h", "4h"]

    @property
    def tg_trade_rules_set(self) -> set[str] | None:
        """None=不限制（全部规则可推 TG）；否则仅集合内规则推 TG。"""
        raw = (self.monitor_tg_trade_rules or "").strip()
        if not raw:
            return None
        return {x.strip().lower() for x in raw.split(",") if x.strip()}

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
