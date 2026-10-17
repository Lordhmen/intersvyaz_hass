"""Данные договора: баланс, следующий платёж, номер договора, услуги.

Отдельный координатор с редким опросом (баланс меняется редко), чтобы
не нагружать API вместе с частым обновлением ссылок на камеры. Ошибки
здесь не должны мешать камерам, кнопкам и звонкам — поэтому первый
запрос при старте не обязателен (см. __init__.py).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.util import dt as dt_util

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import IntersvyazApiClient, IntersvyazApiError, IntersvyazAuthError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ACCOUNT_UPDATE_INTERVAL = timedelta(hours=1)
HISTORY_DAYS = 120  # за сколько дней искать последний платёж


class IntersvyazAccountCoordinator(DataUpdateCoordinator[dict]):
    """self.data = {"user", "balance", "services", "phone",
    "status", "incidents", "history"} — последние три необязательные
    (None, если запрос не удался)."""

    def __init__(self, hass: HomeAssistant, client: IntersvyazApiClient, label: str) -> None:
        self.client = client
        self._user_id: int | None = None
        self._profile_id: int | None = None
        self._phone: int | str | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_account_{label}",
            update_interval=ACCOUNT_UPDATE_INTERVAL,
        )

    async def _async_update_data(self) -> dict:
        try:
            if self._user_id is None or self._profile_id is None:
                info = await self.client.get_token_info()
                self._user_id = info.get("USER_ID")
                self._profile_id = info.get("PROFILE_ID")
                self._phone = info.get("PHONE")
            user, balance, services = await asyncio.gather(
                self.client.get_user_info(self._user_id),
                self.client.get_balance(self._user_id, self._profile_id),
                self.client.get_services(self._user_id, self._profile_id),
            )
            status, incidents, history = await self._async_optional()
        except IntersvyazAuthError as err:
            # Не запускаем повторную авторизацию отсюда: если токен и правда
            # истёк, это сделает основной координатор камер. А 401/403 на
            # профиль/баланс бывает и у «урезанных» токенов (не владелец
            # договора) — камеры и двери при этом работают.
            self._user_id = self._profile_id = None
            raise UpdateFailed(f"Нет доступа к данным договора: {err}") from err
        except IntersvyazApiError as err:
            # Профиль мог смениться — перечитаем идентификаторы в следующий раз.
            self._user_id = self._profile_id = None
            raise UpdateFailed(str(err)) from err
        return {
            "user": user,
            "balance": balance,
            "services": services,
            "phone": self._phone,
            "status": status,
            "incidents": incidents,
            "history": history,
        }

    async def _async_optional(self) -> tuple:
        """Статус сети, аварии и история платежей. Ошибка любого из этих
        запросов не ломает баланс и договор — соответствующий сенсор просто
        станет недоступен."""
        today = dt_util.now().date()
        begin = (today - timedelta(days=HISTORY_DAYS)).isoformat()
        results = await asyncio.gather(
            self.client.get_service_status(self._user_id, self._profile_id),
            self.client.get_incidents(self._user_id, self._profile_id),
            self.client.get_balance_history(
                self._user_id, self._profile_id, begin, today.isoformat()
            ),
            return_exceptions=True,
        )
        out = []
        for name, result in zip(("статус сети", "аварии", "история платежей"), results):
            if isinstance(result, IntersvyazAuthError):
                raise result
            if isinstance(result, Exception):
                _LOGGER.debug("Не удалось получить %s: %s", name, result)
                out.append(None)
            else:
                out.append(result)
        return tuple(out)
