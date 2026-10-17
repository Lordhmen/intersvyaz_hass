"""Sensor platform for Intersvyaz Domofon integration.

Сущности: индикатор "Звонок" (один на запись конфигурации) и данные
договора — баланс и номер договора (на каждый адрес). Остальная FCM-диагностика (sensor.intersvyaz_fcm_status_*, sensor.intersvyaz_last_push_*)
пишется напрямую через hass.states.async_set() в fcm_listener.py
"""
from __future__ import annotations

import logging

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CALL_INDICATOR_RESET_SECONDS = 5


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Один индикатор звонка на запись конфигурации (привязан к первому/
    основному адресу — звонок в домофон один на весь аккаунт, а не на
    каждый отдельный адрес)."""
    accounts: list[dict] = hass.data[DOMAIN][entry.entry_id]["accounts"]
    if not accounts:
        return

    primary = accounts[0]
    entity = IntersvyazCallIndicator(
        hass,
        entry.entry_id,
        primary["device_identifier"],
        primary["address"],
        f"call_indicator_{entry.entry_id}",
    )
    entities: list[SensorEntity] = [entity]

    for account in accounts:
        account_coordinator = account.get("account_coordinator")
        if account_coordinator is None:
            continue
        base = f"{entry.entry_id}{account['unique_suffix']}"
        entities.append(
            IntersvyazBalanceSensor(account_coordinator, account, f"balance_{base}")
        )
        entities.append(
            IntersvyazPaymentDueSensor(account_coordinator, account, f"payment_due_{base}")
        )
        entities.append(
            IntersvyazContractSensor(account_coordinator, account, f"contract_{base}")
        )
        entities.append(
            IntersvyazFullNameSensor(account_coordinator, account, f"full_name_{base}")
        )
        entities.append(
            IntersvyazLoginSensor(account_coordinator, account, f"login_{base}")
        )
        entities.append(
            IntersvyazPhoneSensor(account_coordinator, account, f"phone_{base}")
        )

    async_add_entities(entities)


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ".")) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class _AccountSensor(CoordinatorEntity, SensorEntity):
    """Сенсор данных договора, привязанный к устройству адреса."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, account: dict, unique_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(identifiers={account["device_identifier"]})

    def _payload(self, key: str) -> dict:
        value = (self.coordinator.data or {}).get(key)
        return value if isinstance(value, dict) else {}


def _monthly_fee(data: dict) -> float | None:
    services = data.get("services")
    if not isinstance(services, list):
        return None
    fees = [_as_float(item.get("MONTH_PAY")) for item in services if isinstance(item, dict)]
    fees = [fee for fee in fees if fee is not None]
    return round(sum(fees), 2) if fees else None


class IntersvyazBalanceSensor(_AccountSensor):
    """«Ваш баланс», как в приложении Интерсвязи: деньги на счёте минус
    абонентская плата за следующий месяц."""

    _attr_name = "Баланс"
    _attr_icon = "mdi:cash"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "RUB"
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        on_account = _as_float(self._payload("balance").get("balance"))
        fee = _monthly_fee(self.coordinator.data or {})
        if on_account is None:
            return None
        return round(on_account - (fee or 0), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        balance = self._payload("balance")
        blocked = balance.get("blocked")
        blocked = blocked if isinstance(blocked, dict) else {}
        return {
            "on_account": _as_float(balance.get("balance")),
            "monthly_fee": _monthly_fee(self.coordinator.data or {}),
            "debt": _as_float(balance.get("debt")),
            "blocked": bool(blocked),
            "blocked_text": blocked.get("text"),
            "date_delay_lock": balance.get("dateDelayLock"),
        }


class IntersvyazPaymentDueSensor(_AccountSensor):
    """«К оплате»: абонентская плата за месяц и срок («Оплатите до …»)."""

    _attr_name = "К оплате"
    _attr_icon = "mdi:calendar-clock"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "RUB"
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        return _monthly_fee(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        next_payment = self._payload("balance").get("nextPayment")
        next_payment = next_payment if isinstance(next_payment, dict) else {}
        services = (self.coordinator.data or {}).get("services")
        services = services if isinstance(services, list) else []
        return {
            "due_text": next_payment.get("text"),
            "services": [
                {
                    "name": (item.get("tariff") or {}).get("NAME") or item.get("NAME"),
                    "type": item.get("TYPE"),
                    "month_pay": _as_float(item.get("MONTH_PAY")),
                    "connected": item.get("CONNECT_DATE"),
                }
                for item in services
                if isinstance(item, dict)
            ],
        }


class IntersvyazContractSensor(_AccountSensor):
    """Номер договора и подключённые услуги (ФИО, логин и телефон —
    отдельными сенсорами ниже)."""

    _attr_name = "Договор"
    _attr_icon = "mdi:file-document-outline"

    @property
    def native_value(self) -> str | None:
        account_num = self._payload("user").get("ACCOUNT_NUM")
        return str(account_num) if account_num not in (None, "") else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        user = self._payload("user")
        firm = user.get("firm")
        firm = firm if isinstance(firm, dict) else {}
        return {
            "services": user.get("uslugaList"),
            "role": user.get("roleName"),
            "provider": firm.get("NAME"),
            "active_since": user.get("ACTIVATE_DATE"),
        }


class IntersvyazCallIndicator(SensorEntity):
    """Показывает 'Звонок' на CALL_INDICATOR_RESET_SECONDS секунд при
    предполагаемом звонке в домофон, затем сама сбрасывается в 'Нет'."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:phone-ring"

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        device_identifier: tuple[str, str],
        address: str,
        unique_id: str,
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._attr_name = "Звонок"
        self._attr_unique_id = unique_id
        self._attr_native_value = "Нет"
        self._attr_device_info = DeviceInfo(
            identifiers={device_identifier},
            name=address,
            manufacturer="Интерсвязь",
            model="Домофон IS74",
        )
        self._reset_unsub = None

    async def async_added_to_hass(self) -> None:
        """Регистрируем себя в hass.data, чтобы fcm_listener.py мог найти
        эту сущность и дёрнуть trigger_call() при предполагаемом звонке —
        без прямой связи между модулями, устойчиво к порядку запуска."""
        self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})
        self.hass.data[DOMAIN][self._entry_id]["call_indicator_entity"] = self

    async def async_will_remove_from_hass(self) -> None:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        if entry_data.get("call_indicator_entity") is self:
            entry_data.pop("call_indicator_entity", None)
        if self._reset_unsub:
            self._reset_unsub()
            self._reset_unsub = None

    @callback
    def trigger_call(self) -> None:
        """Вызывается из fcm_listener.py при предполагаемом звонке."""
        self._attr_native_value = "Звонок"
        self.async_write_ha_state()

        if self._reset_unsub:
            self._reset_unsub()

        self._reset_unsub = async_call_later(
            self.hass, CALL_INDICATOR_RESET_SECONDS, self._reset
        )

    @callback
    def _reset(self, _now) -> None:
        self._attr_native_value = "Нет"
        self._reset_unsub = None
        self.async_write_ha_state()

def _text(value: Any) -> str | None:
    return str(value).strip() if value not in (None, "") and str(value).strip() else None


class IntersvyazFullNameSensor(_AccountSensor):
    """ФИО владельца договора из профиля."""

    _attr_name = "ФИО"
    _attr_icon = "mdi:account"

    @property
    def native_value(self) -> str | None:
        # Полное ФИО приходит в profileName; FULL_NAME/shortFio у
        # Интерсвязи бывают сокращёнными (одно слово).
        user = self._payload("user")
        return (
            _text(user.get("profileName"))
            or _text(user.get("FULL_NAME"))
            or _text(user.get("shortFio"))
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        user = self._payload("user")
        return {"short": _text(user.get("shortFio")) or _text(user.get("FULL_NAME"))}


class IntersvyazLoginSensor(_AccountSensor):
    """Логин личного кабинета."""

    _attr_name = "Логин"
    _attr_icon = "mdi:account-key"

    @property
    def native_value(self) -> str | None:
        return _text(self._payload("user").get("LOGIN"))


class IntersvyazPhoneSensor(_AccountSensor):
    """Телефон, к которому привязан токен, в формате +7XXXXXXXXXX."""

    _attr_name = "Телефон"
    _attr_icon = "mdi:phone"

    @property
    def native_value(self) -> str | None:
        digits = "".join(ch for ch in str((self.coordinator.data or {}).get("phone") or "") if ch.isdigit())
        if not digits:
            return None
        if len(digits) == 10:
            digits = "7" + digits
        elif len(digits) == 11 and digits[0] == "8":
            digits = "7" + digits[1:]
        return f"+{digits}"
