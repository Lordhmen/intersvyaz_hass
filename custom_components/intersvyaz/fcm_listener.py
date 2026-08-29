"""FCM-слушатель звонков в домофон.

Интерсвязь шлёт push-уведомление о звонке в домофон как FCM data-пуш.
Обычно такой пуш получает и разбирает установленное Android-приложение —
этот модуль вместо этого регистрирует Home Assistant как ещё одно
"виртуальное" устройство напрямую в Firebase-проекте Интерсвязи
(intersvyazlk) и слушает те же самые пуши без какого-либо реального
телефона.

Регистрация происходит СРАЗУ в двух независимых списках на бэкенде:
  1. api.is74.ru/mobile/pushtoken/add-with-device-id — общий список
  2. td-crm.is74.ru/user-device — отдельный бэкенд именно для звонков
     (нужна отдельная авторизация через auth-lk, buyerId всегда 1)

Классификация пушей: у "обычных" уведомлений (новости, статусы заявок и
т.п.) в payload есть непустое поле "data" с title/text/type. У звонка в
домофон data пустая/отсутствует — "голый конверт". ВНИМАНИЕ: тот же самый
голый вид имеет и фоновый keep-alive FCM, никак не связанный со звонками —
надёжного способа отличить их по содержимому пока не найдено, 
поэтому сейчас каждый "голый" пуш трактуется как предполагаемый
звонок, вкл. вероятность ложных срабатываний на фоновый шум.

Firebase-конфиг проекта (project_id/app_id/api_key/sender_id) — публичные
идентификаторы, зашитые в открытом виде в APK (Google Firebase не считает
их секретом — секретность обеспечивается на уровне бэкенда через Bearer-
токен аккаунта, который выдаётся только после SMS-подтверждения). Тем не
менее весь этот модуль построен на реверс-инжиниринге непубличного
протокола и ничем не гарантирован Интерсвязью — при изменении схемы на их
стороне слушатель может перестать работать.
"""
from __future__ import annotations

import json
import logging
import uuid as uuid_lib
from datetime import datetime, timezone
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import IntersvyazApiClient, IntersvyazApiError
from .const import DOMAIN, EVENT_INCOMING_CALL

_LOGGER = logging.getLogger(__name__)

# Firebase-конфиг проекта intersvyazlk — найден разбором resources.arsc
# из APK com.intersvyaz.lk (версия 2.19.0/2.20.0).
FCM_PROJECT_ID = "intersvyazlk"
FCM_APP_ID = "1:361180765175:android:9c0fafffa6c60062"
FCM_API_KEY = "AIzaSyCWGN-JHGm50OpAo3-2gR7l1kCQIEs7YO4"
FCM_SENDER_ID = "361180765175"

# appId для td-crm — это package name приложения, НЕ Firebase app_id
# (подтверждено перехватом реального трафика официального приложения).
TD_APP_ID = "com.intersvyaz.lk"

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}_fcm_credentials"


class FcmCallListener:
    """Держит соединение с FCM, регистрирует устройство в двух списках,
    классифицирует входящие пуши и пробрасывает предполагаемые звонки."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: IntersvyazApiClient,
        phone: str,
        entry_id: str,
    ) -> None:
        self._hass = hass
        self._client = client
        self._phone = phone
        self._entry_id = entry_id
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry_id}")
        self._fcm_client = None  # type: ignore[assignment]
        self._unique_device_id: str | None = None
        self.fcm_token: str | None = None

        # Колбэк на предполагаемый звонок — привязывается снаружи (см.
        # sensor.py), чтобы включить/выключить индикатор "Звонок" на
        # странице интеграции. Не обязателен: событие EVENT_INCOMING_CALL
        # летит в шину HA в любом случае, для тех, кто хочет строить
        # автоматизации напрямую.
        self.on_call_detected: Callable[[], None] | None = None

        suffix = entry_id.lower()
        self._status_entity_id = f"sensor.intersvyaz_fcm_status_{suffix}"
        self._last_push_entity_id = f"sensor.intersvyaz_last_push_{suffix}"

    def _set_status(self, state: str, **attrs: Any) -> None:
        self._hass.states.async_set(
            self._status_entity_id,
            state,
            {"friendly_name": "Интерсвязь — статус FCM-слушателя", **attrs},
        )

    # ------------------------------------------------------------------ #
    # Запуск: FCM-соединение + регистрация в обоих списках на бэкенде
    # ------------------------------------------------------------------ #
    async def async_start(self) -> None:
        from firebase_messaging import FcmPushClient, FcmRegisterConfig

        self._set_status("подключение")

        stored = await self._store.async_load() or {}
        fcm_creds = stored.get("fcm_credentials")
        cached_td_token = stored.get("td_token")
        self._unique_device_id = stored.get("unique_device_id")
        if not self._unique_device_id:
            self._unique_device_id = uuid_lib.uuid4().hex

        fcm_config = FcmRegisterConfig(FCM_PROJECT_ID, FCM_APP_ID, FCM_API_KEY, FCM_SENDER_ID)

        async def _save_credentials(creds: dict) -> None:
            await self._save_store(fcm_credentials=creds)

        def _on_credentials_updated(creds: dict) -> None:
            self._hass.async_create_task(_save_credentials(creds))

        # ВАЖНО: сигнатура FcmPushClient — (callback, fcm_config, credentials,
        # credentials_updated_callback, ...) — callback обязателен сразу в
        # конструкторе, задать его позже через атрибут нельзя.
        self._fcm_client = FcmPushClient(
            self._on_notification,
            fcm_config,
            fcm_creds,
            _on_credentials_updated,
        )

        fcm_token = await self._fcm_client.checkin_or_register()
        self.fcm_token = fcm_token
        _LOGGER.debug("Получен FCM-токен виртуального устройства (%s...)", fcm_token[:12])
        self._set_status("токен получен, регистрация на бэкенде")

        # Список 1: api.is74.ru (общий push-список)
        try:
            await self._client.register_push_token(
                fcm_token=fcm_token,
                unique_device_id=self._unique_device_id,
                authorize_phone=self._phone,
            )
        except IntersvyazApiError as err:
            _LOGGER.error("Регистрация в api.is74.ru не удалась: %s", err)
            # Не фатально — пробуем и второй список тоже.

        # Список 2: td-crm.is74.ru (нужен отдельный td-токен)
        td_token: str | None = None
        if cached_td_token and await self._client.check_td_token(cached_td_token):
            _LOGGER.info("Переиспользую живой td-токен из кеша (без новой сессии auth-lk)")
            td_token = cached_td_token
        else:
            try:
                td_auth = await self._client.auth_lk()
                td_token = td_auth["TOKEN"]
            except IntersvyazApiError as err:
                _LOGGER.error("auth-lk не удался: %s", err)
                self._set_status("ошибка auth-lk", error=str(err))

        if td_token:
            await self._save_store(td_token=td_token)
            try:
                await self._client.register_td_device(
                    td_token,
                    app_id=TD_APP_ID,
                    push_token=fcm_token,
                    platform="android",
                    device_name="Home Assistant",
                    device_id=self._unique_device_id,
                    sending_push=True,
                    notification_type="push",
                )
                _LOGGER.info("Устройство зарегистрировано в td-crm (user-device)")
            except IntersvyazApiError as err:
                _LOGGER.error("user-device не удался: %s", err)
                self._set_status("ошибка user-device", error=str(err))
                return

        await self._save_store(unique_device_id=self._unique_device_id)
        await self._fcm_client.start()
        self._set_status("слушает", unique_device_id=self._unique_device_id)

    async def _save_store(self, **updates: Any) -> None:
        current = await self._store.async_load() or {}
        current.update(updates)
        await self._store.async_save(current)

    # ------------------------------------------------------------------ #
    # Разбор входящих пушей
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_data(value: Any) -> dict[str, Any] | None:
        """Защищённо достаёт dict с полями пуша из value, каким бы оно ни
        пришло — уже готовым dict, вложенным под "data", JSON-строкой, или
        чем-то совсем неожиданным (тогда просто None, без исключения)."""
        if isinstance(value, dict):
            inner = value.get("data")
            return inner if isinstance(inner, dict) else value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

    def _on_notification(self, obj: Any, notification: Any, data_message: Any) -> None:
        """Callback firebase-messaging на каждый входящий пуш.

        ВАЖНО: obj/notification могут прийти НЕ словарями (например, для
        чисто data-пушей без видимого уведомления notification бывает
        строкой) — весь разбор ниже защищён от этого явно, без опоры на
        try/except, чтобы push не терялся молча при неожиданной форме.
        """
        data = self._extract_data(obj) or self._extract_data(notification) or {}
        has_data = isinstance(obj, dict) and bool(obj.get("data"))
        now_iso = datetime.now(timezone.utc).isoformat()
        message_id = obj.get("fcmMessageId") if isinstance(obj, dict) else None

        _LOGGER.debug(
            "Входящий FCM-пуш: has_data=%s obj=%r notification=%r",
            has_data, obj, notification,
        )

        # "Голый конверт" (нет data) -> предполагаемый звонок. Есть data ->
        # обычное уведомление (новость, статус заявки и т.п.).
        assumed_call = not has_data

        self._hass.states.async_set(
            self._last_push_entity_id,
            (data.get("type") or data.get("push_type") or ("ЗВОНОК?" if assumed_call else "unknown")),
            {
                "friendly_name": "Интерсвязь — последний пуш",
                "received_at": now_iso,
                "assumed_call": assumed_call,
                "has_data_field": has_data,
                "message_id": message_id,
                "data_type": data.get("type"),
                "push_type": data.get("push_type"),
                "title": data.get("title"),
                "text": data.get("text"),
                "raw_obj": obj,
                "raw_notification": notification,
            },
        )

        if assumed_call:
            _LOGGER.info("Предполагаемый звонок в домофон (пуш без data)")
            self._hass.bus.async_fire(EVENT_INCOMING_CALL, {"source": "fcm", "message_id": message_id})
            if self.on_call_detected:
                self.on_call_detected()

    async def async_stop(self) -> None:
        if self._fcm_client is not None:
            await self._fcm_client.stop()
        self._set_status("остановлен")