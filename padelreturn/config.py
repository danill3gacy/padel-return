"""Конфигурация. Всё, что крутится от клуба к клубу, лежит здесь и в clubs.settings_json."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


@dataclass
class Config:
    # --- сегментация ---
    sleeping_min_days: int = 45          # абсолютный минимум "молчания"
    sleeping_interval_mult: float = 3.0  # или 3x личного ритма — что больше
    newbie_max_visits: int = 3
    loyal_min_visits: int = 8
    no_show_problem_rate: float = 0.30
    recent_contact_days: int = 30        # кому писали недавно — не трогаем
    seasonal_min_visits: int = 6

    # --- контрольная группа ---
    control_share: float = 0.10

    # --- кампания ---
    wave_size: int = 50                  # не больше N первых касаний в день
    touch_2_delay_days: int = 5
    touch_3_delay_days: int = 12
    max_touches: int = 3
    quiet_hours: tuple[int, int] = (21, 10)   # с 21:00 до 10:00 не пишем

    # --- офферы ---
    offer_horizon_days: int = 10
    level_window: float = 0.5            # допустимый разброс уровня в четвёрке
    seats_per_offer: int = 4

    # --- атрибуция ---
    return_window_days: int = 21
    revenue_window_days: int = 60

    # --- каналы ---
    default_channel: str = _env("PADEL_CHANNEL", "console")
    channel_fallback: str = "sms"
    cost_whatsapp: float = 7.0
    cost_sms: float = 3.0
    cost_telegram: float = 0.0

    # --- LLM ---
    llm_provider: str = _env("PADEL_LLM_PROVIDER", "none")   # none | anthropic | openai
    llm_model: str = _env("PADEL_LLM_MODEL", "claude-sonnet-4-5")
    llm_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY") or _env("OPENAI_API_KEY"))
    llm_base_url: str = _env("PADEL_LLM_BASE_URL", "")
    llm_timeout: int = 45

    # --- провайдер WhatsApp (Wazzup / Radist и т.п.) ---
    wa_api_url: str = _env("PADEL_WA_URL", "")
    wa_api_key: str = _env("PADEL_WA_KEY", "")
    wa_channel_id: str = _env("PADEL_WA_CHANNEL", "")

    # --- SMS ---
    sms_api_url: str = _env("PADEL_SMS_URL", "")
    sms_api_key: str = _env("PADEL_SMS_KEY", "")
    sms_sender: str = _env("PADEL_SMS_SENDER", "PADEL")

    # --- Telegram ---
    tg_bot_token: str = _env("PADEL_TG_TOKEN", "")
    tg_admin_chat: str = _env("PADEL_TG_ADMIN_CHAT", "")

    # --- режим ---
    require_approval: bool = True        # human-in-the-loop: подтверждение перед отправкой
    dry_run: bool = _env("PADEL_DRY_RUN", "0") == "1"

    def merged(self, overrides: dict | None) -> Config:
        if not overrides:
            return self
        data = asdict(self)
        for k, v in overrides.items():
            if k in data:
                data[k] = v
        return Config(**data)

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider != "none" and bool(self.llm_api_key)


CONFIG = Config()
