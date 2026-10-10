"""Патч расшифровки data-пушей в firebase-messaging.

Проблема (выявлено 2026-10-11, firebase-messaging 0.4.5): библиотека берёт
заголовок crypto-key как "dh=<base64>" и отрезает первые 3 символа,
а encryption — как "salt=<base64>" с отрезанием 5 символов. Если сервер
присылает заголовок в другом виде (например "dh=...;p256ecdsa=..." в
любом порядке) или base64 без завершающих "=", urlsafe_b64decode падает
с "Incorrect padding", исключение вылетает из _listen и библиотека
ПОЛНОСТЬЮ останавливает FcmPushClient — следующие звонки теряются
до переподключения.

Патч:
  1. Вытаскивает значение dh / salt из заголовка с несколькими
     параметрами через ";" и добивает base64-padding.
  2. Если расшифровать всё равно не удалось — НЕ роняет клиента, а
     отдаёт пустой JSON "{}". В fcm_listener пуш без data трактуется как
     звонок, так что звонок всё равно будет зафиксирован.
"""
from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

_PATCHED_ATTR = "_intersvyaz_patched"


def _pad(value: str) -> str:
    value = value.strip()
    return value + "=" * (-len(value) % 4)


def _extract(header_tail: str, key: str) -> str:
    """header_tail — то, что осталось после того, как библиотека отрезала
    префикс "dh=" / "salt=". Ищем сегмент key=..., иначе берём первый."""
    parts = [p.strip() for p in header_tail.split(";") if p.strip()]
    if not parts:
        return header_tail
    prefix = f"{key}="
    for part in parts:
        if part.startswith(prefix):
            return part[len(prefix):]
    return parts[0]


def apply() -> None:
    from firebase_messaging import fcmpushclient

    cls = fcmpushclient.FcmPushClient
    if getattr(cls, _PATCHED_ATTR, False):
        return

    original = cls._decrypt_raw_data  # staticmethod -> обычная функция

    def _decrypt_raw_data(credentials, crypto_key_str, salt_str, raw_data):
        _LOGGER.debug(
            "FCM decrypt: crypto-key=%r salt=%r raw_len=%s",
            crypto_key_str, salt_str, len(raw_data) if raw_data else 0,
        )
        try:
            return original(
                credentials,
                _pad(_extract(crypto_key_str, "dh")),
                _pad(_extract(salt_str, "salt")),
                raw_data,
            )
        except Exception as err:  # noqa: BLE001 — один кривой пуш не должен убивать слушатель
            _LOGGER.warning(
                "Не удалось расшифровать FCM-пуш (%s: %s); crypto-key=%r salt=%r — "
                "передаю как пуш без данных",
                type(err).__name__, err, crypto_key_str, salt_str,
            )
            return b"{}"

    cls._decrypt_raw_data = staticmethod(_decrypt_raw_data)
    setattr(cls, _PATCHED_ATTR, True)
    _LOGGER.debug("Патч firebase_messaging._decrypt_raw_data применён")
