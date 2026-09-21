"""Interactive login for `python -m tgsender session`.

Replaces Telethon's `client.start()`, which has two problems for this use:

* it passes the phone through as typed, and a Russian domestic number like
  89991234567 gets a code sent but then fails at sign-in;
* it keeps the `phone_code_hash` only in client state, so if the connection
  drops between sending the code and checking it (Telegram does this), the
  retry dies with "You also need to provide a phone_code_hash".

Here the hash is held explicitly and passed on every attempt.
"""

from __future__ import annotations

import getpass
import re

from telethon import TelegramClient, errors
from telethon.tl.types import auth

MAX_CODE_ATTEMPTS = 3
MAX_PASSWORD_ATTEMPTS = 3


class LoginError(Exception):
    """Something the user must fix; the message says what."""


def normalize_phone(raw: str) -> str:
    """Accept what people actually type and return +<international digits>."""
    text = raw.strip()
    if ":" in text and re.search(r"[A-Za-z]", text):
        raise LoginError(
            "Это похоже на токен бота. Здесь нужен НОМЕР ТЕЛЕФОНА вашего личного "
            "аккаунта, например +79991234567."
        )
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        pass
    elif len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]      # 8 999 … is the domestic form of +7 999 …
    elif len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits          # a Russian mobile typed without the prefix
    if not 10 <= len(digits) <= 15:
        raise LoginError(f"Не похоже на номер телефона: «{raw.strip()}».")
    return "+" + digits


def _pretty(phone: str) -> str:
    d = phone[1:]
    if len(d) == 11 and d[0] == "7":
        return f"+7 {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:]}"
    return phone


def _where(sent: auth.SentCode) -> str:
    kind = type(sent.type).__name__
    return {
        "SentCodeTypeApp": "в приложение Telegram — чат «Telegram» на другом устройстве",
        "SentCodeTypeSms": "по SMS",
        "SentCodeTypeCall": "звонком — код продиктуют",
        "SentCodeTypeFlashCall": "сброшенным звонком — код в номере звонящего",
        "SentCodeTypeMissedCall": "пропущенным звонком — код в последних цифрах номера",
        "SentCodeTypeEmailCode": "на email, привязанный к аккаунту",
        "SentCodeTypeFragmentSms": "через Fragment",
    }.get(kind, "в Telegram")


def _clean_code(raw: str) -> str:
    return re.sub(r"\D", "", raw)


async def interactive_login(client: TelegramClient, ask=input, ask_secret=getpass.getpass):
    """Log `client` in as a person. Returns the signed-in user."""
    await client.connect()

    phone = None
    while phone is None:
        try:
            phone = normalize_phone(
                ask("Номер телефона личного аккаунта (например +79991234567): ")
            )
        except LoginError as exc:
            print(f"  ⚠️  {exc}")

    print(f"\nОтправляю код на {_pretty(phone)}…")
    try:
        sent = await client.send_code_request(phone)
    except errors.PhoneNumberInvalidError as exc:
        raise LoginError(f"Telegram не принял номер {_pretty(phone)}.") from exc
    except errors.PhoneNumberBannedError as exc:
        raise LoginError("Этот номер заблокирован в Telegram.") from exc
    except errors.FloodWaitError as exc:
        raise LoginError(
            f"Слишком много попыток входа. Telegram просит подождать "
            f"{exc.seconds // 60} мин, потом запусти команду снова."
        ) from exc
    print(f"Код отправлен {_where(sent)}.\n")

    code_hash = sent.phone_code_hash
    attempts = 0
    while True:
        code = _clean_code(ask("Код из Telegram: "))
        if not code:
            continue
        try:
            # The hash is passed explicitly, so a reconnect in between is harmless.
            return await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
        except errors.SessionPasswordNeededError:
            break
        except errors.PhoneCodeInvalidError:
            attempts += 1
            if attempts >= MAX_CODE_ATTEMPTS:
                raise LoginError(
                    "Код трижды не подошёл. Запусти команду заново — придёт новый код."
                ) from None
            print(f"  ⚠️  Неверный код. Осталось попыток: {MAX_CODE_ATTEMPTS - attempts}.")
        except errors.PhoneCodeExpiredError:
            print("  ⚠️  Код истёк. Отправляю новый…")
            sent = await client.send_code_request(phone)
            code_hash = sent.phone_code_hash
            print(f"Новый код отправлен {_where(sent)}.\n")
        except errors.PhoneNumberUnoccupiedError as exc:
            raise LoginError(f"На номер {_pretty(phone)} нет аккаунта Telegram.") from exc

    # Two-step verification (the "cloud password").
    print("\nУ аккаунта включена двухэтапная аутентификация.")
    print("Нужен облачный пароль: Настройки → Конфиденциальность → Двухэтапная аутентификация.")
    print("При вводе символы не отображаются — это нормально.\n")
    for left in range(MAX_PASSWORD_ATTEMPTS, 0, -1):
        password = ask_secret("Облачный пароль: ")
        try:
            return await client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            if left == 1:
                raise LoginError("Пароль трижды не подошёл.") from None
            print(f"  ⚠️  Неверный пароль. Осталось попыток: {left - 1}.")
