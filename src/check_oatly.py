from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


PAGE_URL = "https://www.aktionspreis.de/angebote/oatly-barista-1l"
STATE_FILE = Path(__file__).resolve().parent.parent / "state.json"
REQUEST_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (compatible; OatlyAlertBot/1.0; "
    "+https://github.com)"
)
MESSAGE_PREFIX = "Oatly Barista Angebotsalarm"
SUBSCRIPTION_COMMANDS = {"/start", "/subscribe"}
UNSUBSCRIBE_COMMANDS = {"/unsubscribe"}
INFO_COMMANDS = {"/help", "/status"}


@dataclass
class OfferSnapshot:
    checked_at: str
    page_url: str
    current_offer_present: bool
    offer_count: int
    best_price: str | None
    merchants: list[str]


@dataclass
class Subscriber:
    chat_id: str
    chat_type: str
    label: str
    last_seen_at: str


class OatlyCheckError(Exception):
    pass


class TelegramAPIError(OatlyCheckError):
    def __init__(self, message: str, *, status_code: int | None = None, response_data: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data

    def is_chat_unavailable(self) -> bool:
        description = ""
        if isinstance(self.response_data, dict):
            description = str(self.response_data.get("description", "")).lower()

        return self.status_code == 403 or "chat not found" in description or "bot was blocked" in description


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_page(url: str) -> str:
    logging.info("Rufe Seite ab: %s", url)
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise OatlyCheckError(f"Seite konnte nicht abgerufen werden: {exc}") from exc

    return response.text


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_decimal(value: str) -> Decimal:
    normalized = value.replace(".", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise OatlyCheckError(f"Ungueltiger Preiswert: {value}") from exc


def format_decimal(value: Decimal) -> str:
    return f"{value:.2f}"


def extract_offer_count(text: str) -> int:
    if re.search(r"\bein Angebot\b", text, flags=re.IGNORECASE):
        return 1

    match = re.search(r"\b(\d+)\s+Angebote\b", text, flags=re.IGNORECASE)
    if not match:
        return 0
    return int(match.group(1))


def extract_best_price(text: str) -> Decimal | None:
    current_offer_block = text.split("letzte Aktion", 1)[0]
    patterns = [
        r"Tiefstpreis(?:[^0-9]+)(\d+,\d{2})\s*€",
        r"Unter allen .*? ist (\d+,\d{2})\s*€\s+der aktuell günstigste",
        r"\b(?:\d+\s+Angebote|ein Angebot)\b.*?(?:ab\s+)?(\d+,\d{2})\s*€",
        r"ist\s+(\d+,\d{2})\s*€\s+der aktuell günstigste",
    ]

    for pattern in patterns:
        match = re.search(pattern, current_offer_block, flags=re.IGNORECASE)
        if match:
            return parse_decimal(match.group(1))

    active_offer_prices = re.findall(
        (
            r"(?:noch \d+ Tage gültig|nur noch heute gültig|ab morgen gültig|"
            r"in \d+ Tagen gültig|heute gültig|gültig bis \d{2}\.\d{2}\.\d{2})"
            r".{0,80}?(\d+,\d{2})\s*€"
        ),
        text,
        flags=re.IGNORECASE,
    )
    if active_offer_prices:
        return min(parse_decimal(price) for price in active_offer_prices)

    return None


def split_merchants(raw_value: str) -> list[str]:
    cleaned = raw_value.replace(" und ", ", ")
    merchants = []
    for part in cleaned.split(","):
        merchant = part.strip(" .")
        if merchant and merchant not in merchants:
            merchants.append(merchant)
    return merchants


def extract_merchants(text: str) -> list[str]:
    patterns = [
        r"Im Moment gibt es Oatly Barista Angebote(?: bzw\. Oatly Barista Werbung)? bei (.+?)\.\s",
        r"aktuelle Oatly Barista Angebote(?: bzw\. Oatly Barista Werbung)? bei (.+?)\.\s",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return split_merchants(match.group(1))
    return []


def parse_snapshot(html: str) -> OfferSnapshot:
    text = normalize_text(extract_text(html))
    offer_count = extract_offer_count(text)
    best_price = extract_best_price(text)
    merchants = extract_merchants(text)
    current_offer_present = offer_count > 0

    snapshot = OfferSnapshot(
        checked_at=utc_now_iso(),
        page_url=PAGE_URL,
        current_offer_present=current_offer_present,
        offer_count=offer_count,
        best_price=format_decimal(best_price) if best_price is not None else None,
        merchants=sorted(merchants),
    )

    logging.info(
        "Analyse abgeschlossen: offer_present=%s, offer_count=%s, best_price=%s, merchants=%s",
        snapshot.current_offer_present,
        snapshot.offer_count,
        snapshot.best_price,
        ", ".join(snapshot.merchants) if snapshot.merchants else "-",
    )
    return snapshot


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        logging.info("Noch keine state.json gefunden.")
        return {}

    try:
        raw_state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OatlyCheckError(f"state.json konnte nicht gelesen werden: {exc}") from exc

    if not isinstance(raw_state, dict):
        raise OatlyCheckError("state.json hat kein gueltiges JSON-Objekt.")

    return raw_state


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise OatlyCheckError(f"state.json konnte nicht geschrieben werden: {exc}") from exc


def normalize_state(raw_state: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "offer": None,
        "telegram": {
            "last_update_id": 0,
            "subscribers": [],
        },
    }

    if "offer" in raw_state or "telegram" in raw_state:
        offer = raw_state.get("offer")
        telegram = raw_state.get("telegram")
        if isinstance(offer, dict):
            state["offer"] = offer
        if isinstance(telegram, dict):
            state["telegram"]["last_update_id"] = int(telegram.get("last_update_id", 0) or 0)
            state["telegram"]["subscribers"] = coerce_subscribers(telegram.get("subscribers", []))
        return state

    if any(key in raw_state for key in {"current_offer_present", "offer_count", "best_price", "merchants"}):
        state["offer"] = raw_state

    return state


def coerce_subscribers(raw_subscribers: Any) -> list[dict[str, str]]:
    subscribers: list[dict[str, str]] = []
    for raw_subscriber in raw_subscribers if isinstance(raw_subscribers, list) else []:
        if isinstance(raw_subscriber, dict) and raw_subscriber.get("chat_id"):
            subscribers.append(
                {
                    "chat_id": str(raw_subscriber["chat_id"]),
                    "chat_type": str(raw_subscriber.get("chat_type", "private")),
                    "label": str(raw_subscriber.get("label", raw_subscriber["chat_id"])),
                    "last_seen_at": str(raw_subscriber.get("last_seen_at", utc_now_iso())),
                }
            )
        elif raw_subscriber:
            subscribers.append(
                {
                    "chat_id": str(raw_subscriber),
                    "chat_type": "private",
                    "label": str(raw_subscriber),
                    "last_seen_at": utc_now_iso(),
                }
            )
    return subscribers


def determine_changes(previous: dict[str, Any] | None, current: OfferSnapshot) -> list[str]:
    if not current.current_offer_present:
        return []

    if previous is None:
        return ["Erstmals ein aktuelles Angebot erkannt"]

    changes: list[str] = []
    if not previous.get("current_offer_present"):
        changes.append("Erstmals wieder ein aktuelles Angebot erkannt")
    if previous.get("best_price") != current.best_price:
        changes.append(f"Preis geaendert: {previous.get('best_price')} -> {current.best_price} EUR")
    if int(previous.get("offer_count", 0)) != current.offer_count:
        changes.append(
            f"Anzahl der Angebote geaendert: {previous.get('offer_count', 0)} -> {current.offer_count}"
        )
    if sorted(previous.get("merchants", [])) != current.merchants:
        old_merchants = ", ".join(previous.get("merchants", [])) or "-"
        new_merchants = ", ".join(current.merchants) or "-"
        changes.append(f"Haendler geaendert: {old_merchants} -> {new_merchants}")

    return changes


def load_price_threshold() -> Decimal | None:
    raw_threshold = os.getenv("PRICE_THRESHOLD", "").strip()
    if not raw_threshold:
        return None
    return parse_decimal(raw_threshold)


def load_bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def should_notify(changes: list[str], snapshot: OfferSnapshot, threshold: Decimal | None) -> bool:
    if not changes:
        return False

    if threshold is None:
        return True

    if snapshot.best_price is None:
        return False

    return Decimal(snapshot.best_price) <= threshold


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise OatlyCheckError(f"Pflichtvariable fehlt: {name}")
    return value


def build_offer_message(snapshot: OfferSnapshot, changes: list[str], threshold: Decimal | None) -> str:
    merchants = ", ".join(snapshot.merchants) if snapshot.merchants else "keine erkannt"
    message_lines = [
        MESSAGE_PREFIX,
        "",
        "Aenderungen:",
        *[f"- {change}" for change in changes],
        "",
        f"Angebot aktiv: {'ja' if snapshot.current_offer_present else 'nein'}",
        f"Anzahl Angebote: {snapshot.offer_count}",
        f"Tiefstpreis: {snapshot.best_price or 'unbekannt'} EUR",
        f"Im Angebot bei: {merchants}",
        f"Quelle: {snapshot.page_url}",
    ]

    if threshold is not None:
        message_lines.append(f"Preisgrenze: {format_decimal(threshold)} EUR")

    return "\n".join(message_lines)


def build_subscription_label(chat: dict[str, Any]) -> str:
    title = str(chat.get("title", "")).strip()
    if title:
        return title

    username = str(chat.get("username", "")).strip()
    if username:
        return f"@{username}"

    first_name = str(chat.get("first_name", "")).strip()
    last_name = str(chat.get("last_name", "")).strip()
    full_name = " ".join(part for part in [first_name, last_name] if part)
    return full_name or str(chat.get("id", "unbekannt"))


def build_subscription_message(chat: dict[str, Any]) -> str:
    label = build_subscription_label(chat)
    return "\n".join(
        [
            "Oatly Alert Bot",
            "",
            f"{label} ist jetzt fuer Angebotsalarme registriert.",
            "Befehle:",
            "- /subscribe oder /start: Abo aktivieren",
            "- /unsubscribe: Abo beenden",
            "- /status: aktuellen Status anzeigen",
        ]
    )


def build_unsubscribe_message(label: str) -> str:
    return "\n".join(
        [
            "Oatly Alert Bot",
            "",
            f"{label} wurde von den Angebotsalarmen abgemeldet.",
            "Mit /subscribe kannst du das Abo jederzeit wieder aktivieren.",
        ]
    )


def build_status_message(is_subscribed: bool, subscriber_count: int) -> str:
    status_text = "aktiv" if is_subscribed else "nicht aktiv"
    return "\n".join(
        [
            "Oatly Alert Bot",
            "",
            f"Dein Abo ist aktuell: {status_text}",
            f"Aktive Abonnenten insgesamt: {subscriber_count}",
            "",
            "Befehle:",
            "- /subscribe oder /start",
            "- /unsubscribe",
        ]
    )


def telegram_api_request(bot_token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    api_url = f"https://api.telegram.org/bot{bot_token}/{method}"

    try:
        response = requests.post(api_url, json=payload or {}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise TelegramAPIError(f"Telegram-Aufruf {method} fehlgeschlagen: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise TelegramAPIError(
            f"Telegram-Aufruf {method} hat keine gueltige JSON-Antwort geliefert.",
            status_code=response.status_code,
        ) from exc

    if not response.ok or not data.get("ok"):
        raise TelegramAPIError(
            f"Telegram API meldet Fehler bei {method}: {data}",
            status_code=response.status_code,
            response_data=data,
        )

    return data.get("result")


def send_telegram_message(bot_token: str, chat_id: str, message: str) -> None:
    telegram_api_request(
        bot_token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        },
    )


def fetch_telegram_updates(bot_token: str, offset: int) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message"]}
    if offset > 0:
        payload["offset"] = offset

    result = telegram_api_request(bot_token, "getUpdates", payload)
    if not isinstance(result, list):
        raise OatlyCheckError("Telegram getUpdates hat kein gueltiges Ergebnis geliefert.")
    return [update for update in result if isinstance(update, dict)]


def normalize_command(text: str) -> str:
    command = text.strip().split(maxsplit=1)[0]
    return command.split("@", 1)[0].lower()


def build_subscriber(chat: dict[str, Any]) -> Subscriber:
    return Subscriber(
        chat_id=str(chat["id"]),
        chat_type=str(chat.get("type", "private")),
        label=build_subscription_label(chat),
        last_seen_at=utc_now_iso(),
    )


def process_telegram_updates(bot_token: str, telegram_state: dict[str, Any]) -> dict[str, Any]:
    last_update_id = int(telegram_state.get("last_update_id", 0) or 0)
    subscribers_by_id = {
        subscriber["chat_id"]: subscriber for subscriber in coerce_subscribers(telegram_state.get("subscribers", []))
    }

    updates = fetch_telegram_updates(bot_token, last_update_id + 1 if last_update_id else 0)
    logging.info("Telegram-Updates gefunden: %s", len(updates))

    for update in updates:
        update_id = int(update.get("update_id", 0) or 0)
        if update_id > last_update_id:
            last_update_id = update_id

        message = update.get("message")
        if not isinstance(message, dict):
            continue

        text = str(message.get("text", "")).strip()
        if not text.startswith("/"):
            continue

        chat = message.get("chat")
        if not isinstance(chat, dict) or "id" not in chat:
            continue

        chat_id = str(chat["id"])
        command = normalize_command(text)
        subscriber_count = len(subscribers_by_id)

        if command in SUBSCRIPTION_COMMANDS:
            subscriber = build_subscriber(chat)
            subscribers_by_id[chat_id] = asdict(subscriber)
            send_telegram_message(bot_token, chat_id, build_subscription_message(chat))
            logging.info("Chat %s wurde registriert.", chat_id)
        elif command in UNSUBSCRIBE_COMMANDS:
            previous = subscribers_by_id.pop(chat_id, None)
            label = previous["label"] if previous else build_subscription_label(chat)
            send_telegram_message(bot_token, chat_id, build_unsubscribe_message(label))
            logging.info("Chat %s wurde abgemeldet.", chat_id)
        elif command in INFO_COMMANDS:
            send_telegram_message(
                bot_token,
                chat_id,
                build_status_message(chat_id in subscribers_by_id, subscriber_count),
            )
            logging.info("Status an Chat %s gesendet.", chat_id)

    return {
        "last_update_id": last_update_id,
        "subscribers": sorted(subscribers_by_id.values(), key=lambda item: item["chat_id"]),
    }


def collect_recipients(telegram_state: dict[str, Any], fallback_chat_id: str | None) -> list[str]:
    recipients = {subscriber["chat_id"] for subscriber in coerce_subscribers(telegram_state.get("subscribers", []))}
    if fallback_chat_id:
        recipients.add(fallback_chat_id)
    return sorted(recipients)


def send_alerts(
    bot_token: str,
    recipients: list[str],
    message: str,
    telegram_state: dict[str, Any],
) -> dict[str, Any]:
    subscribers_by_id = {
        subscriber["chat_id"]: subscriber for subscriber in coerce_subscribers(telegram_state.get("subscribers", []))
    }
    failed_recipients: list[str] = []

    for chat_id in recipients:
        try:
            send_telegram_message(bot_token, chat_id, message)
            logging.info("Benachrichtigung an Chat %s gesendet.", chat_id)
        except TelegramAPIError as exc:
            if exc.is_chat_unavailable() and chat_id in subscribers_by_id:
                logging.warning("Chat %s ist nicht mehr erreichbar und wird entfernt.", chat_id)
                subscribers_by_id.pop(chat_id, None)
                continue
            failed_recipients.append(chat_id)
            logging.error("Benachrichtigung an Chat %s fehlgeschlagen: %s", chat_id, exc)

    if failed_recipients:
        raise OatlyCheckError(f"Benachrichtigung fehlgeschlagen fuer Chats: {', '.join(failed_recipients)}")

    telegram_state["subscribers"] = sorted(subscribers_by_id.values(), key=lambda item: item["chat_id"])
    return telegram_state


def main() -> int:
    configure_logging()

    threshold = load_price_threshold()
    force_test_message = load_bool_env("FORCE_TEST_MESSAGE")
    bot_token = require_env("TELEGRAM_BOT_TOKEN")
    fallback_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None

    state = normalize_state(load_state(STATE_FILE))
    state["telegram"] = process_telegram_updates(bot_token, state["telegram"])
    save_state(STATE_FILE, state)
    logging.info("Telegram-Zustand aktualisiert.")

    previous_offer_state = state.get("offer")
    html = fetch_page(PAGE_URL)
    current_snapshot = parse_snapshot(html)
    changes = determine_changes(previous_offer_state, current_snapshot)

    if force_test_message:
        logging.info("FORCE_TEST_MESSAGE aktiv: Sende Testnachricht unabhaengig von Aenderungen.")
        changes = ["Manuell erzwungene Testnachricht"]

    recipients = collect_recipients(state["telegram"], fallback_chat_id)
    if force_test_message or should_notify(changes, current_snapshot, threshold):
        if not recipients:
            logging.info("Keine Empfaenger registriert. Es wird keine Benachrichtigung gesendet.")
        else:
            message = build_offer_message(current_snapshot, changes, threshold)
            state["telegram"] = send_alerts(bot_token, recipients, message, state["telegram"])
            logging.info("Benachrichtigung gesendet.")
    else:
        logging.info("Keine Benachrichtigung erforderlich.")

    state["offer"] = asdict(current_snapshot)
    save_state(STATE_FILE, state)
    logging.info("state.json aktualisiert: %s", STATE_FILE)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OatlyCheckError as exc:
        logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
        logging.error("%s", exc)
        raise SystemExit(1) from exc
    except Exception as exc:  # pragma: no cover
        logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
        logging.exception("Unerwarteter Fehler: %s", exc)
        raise SystemExit(1) from exc
