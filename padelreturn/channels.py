"""Абстракция канала доставки.

Заложена с первого дня намеренно: холодное касание в РФ идёт через WhatsApp Business API,
Telegram-бот доступен только тем, кто уже нажал /start, SMS — фолбэк.
Смена провайдера не должна переписывать половину кода.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import Config


@dataclass
class SendResult:
    ok: bool
    channel: str
    provider_id: str | None = None
    error: str | None = None
    cost: float = 0.0


class Channel:
    name = "base"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def available_for(self, contact) -> bool:
        raise NotImplementedError

    def send(self, contact, text: str, template_key: str | None = None) -> SendResult:
        raise NotImplementedError


class ConsoleChannel(Channel):
    """Режим M0 из PRD: ничего не отправляем, всё печатаем и складываем в БД.

    Именно с него начинается первая кампания: сообщения выгружаются и отправляются
    руками с телефона клуба.
    """
    name = "console"

    def available_for(self, contact) -> bool:
        return True

    def send(self, contact, text: str, template_key: str | None = None) -> SendResult:
        print(f"\n--- [{contact['name']} | {contact['phone']}] ---\n{text}")
        return SendResult(ok=True, channel=self.name, provider_id="console", cost=0.0)


class TelegramChannel(Channel):
    """Бесплатно и лучше всех конвертит, но работает только с теми, кто уже в боте."""
    name = "telegram"

    def available_for(self, contact) -> bool:
        return bool(contact["tg_chat_id"]) and bool(self.cfg.tg_bot_token)

    def send(self, contact, text: str, template_key: str | None = None) -> SendResult:
        if self.cfg.dry_run:
            return SendResult(True, self.name, "dry-run")
        url = f"https://api.telegram.org/bot{self.cfg.tg_bot_token}/sendMessage"
        payload = {"chat_id": contact["tg_chat_id"], "text": text, "disable_web_page_preview": True}
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            return SendResult(bool(data.get("ok")), self.name,
                              str(data.get("result", {}).get("message_id")), cost=self.cfg.cost_telegram)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            return SendResult(False, self.name, error=str(e))


class WhatsAppChannel(Channel):
    """Единственный рабочий холодный канал. Вне 24-часового окна — только шаблоны."""
    name = "whatsapp"

    def available_for(self, contact) -> bool:
        if self.cfg.dry_run:
            return bool(contact["phone"])
        return bool(contact["phone"]) and bool(self.cfg.wa_api_url and self.cfg.wa_api_key)

    def send(self, contact, text: str, template_key: str | None = None) -> SendResult:
        if self.cfg.dry_run:
            return SendResult(True, self.name, "dry-run", cost=self.cfg.cost_whatsapp)
        payload = {
            "channelId": self.cfg.wa_channel_id,
            "chatType": "whatsapp",
            "chatId": contact["phone"].lstrip("+"),
            "text": text,
        }
        if template_key:
            payload["templateId"] = template_key
        try:
            req = urllib.request.Request(
                self.cfg.wa_api_url,
                data=json.dumps(payload).encode(),
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {self.cfg.wa_api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read())
            return SendResult(True, self.name, str(data.get("messageId")), cost=self.cfg.cost_whatsapp)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            return SendResult(False, self.name, error=str(e))


class SmsChannel(Channel):
    name = "sms"

    def available_for(self, contact) -> bool:
        if self.cfg.dry_run:
            return bool(contact["phone"])
        return bool(contact["phone"]) and bool(self.cfg.sms_api_url and self.cfg.sms_api_key)

    def send(self, contact, text: str, template_key: str | None = None) -> SendResult:
        if self.cfg.dry_run:
            return SendResult(True, self.name, "dry-run", cost=self.cfg.cost_sms)
        payload = {
            "sender": self.cfg.sms_sender,
            "to": contact["phone"],
            "text": text[:300],
            "api_key": self.cfg.sms_api_key,
        }
        try:
            req = urllib.request.Request(
                self.cfg.sms_api_url,
                data=json.dumps(payload).encode(),
                headers={"content-type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            return SendResult(True, self.name, str(data.get("id")), cost=self.cfg.cost_sms)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            return SendResult(False, self.name, error=str(e))


REGISTRY = {
    c.name: c for c in (ConsoleChannel, TelegramChannel, WhatsAppChannel, SmsChannel)
}


def pick(cfg: Config, contact) -> Channel:
    """Telegram, если человек уже в боте (бесплатно). Иначе основной канал. Иначе фолбэк."""
    tg = TelegramChannel(cfg)
    if tg.available_for(contact):
        return tg
    primary = REGISTRY.get(cfg.default_channel, ConsoleChannel)(cfg)
    if primary.available_for(contact):
        return primary
    fallback = REGISTRY.get(cfg.channel_fallback, ConsoleChannel)(cfg)
    if fallback.available_for(contact):
        return fallback
    return ConsoleChannel(cfg)
