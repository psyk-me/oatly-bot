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


@dataclass
class OfferSnapshot:
    checked_at: str
    page_url: str
    current_offer_present: bool
    offer_count: int
    best_price: str | None
    merchants: list[str]


class OatlyCheckError(Exception):
    pass


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


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
    match = re.search(r"\b(\d+)\s+Angebote\b", text, flags=re.IGNORECASE)
    if not match:
        return 0
    return int(match.group(1))


def extract_best_price(text: str) -> Decimal | None:
    patterns = [
        r"Tiefstpreis(?:[^0-9]+)(\d+,\d{2})\s*€",
        r"Unter allen .*? ist (\d+,\d{2})\s*€\s+der aktuell günstigste",
        r"ist\s+(\d+,\d{2})\s*€\s+der aktuell günstigste",
        r"\bab\s+(\d+,\d{2})\s*€\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return parse_decimal(match.group(1))

    all_prices = re.findall(r"\b(\d+,\d{2})\s*€", text)
    if all_prices:
        return min(parse_decimal(price) for price in all_prices)

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
        checked_at=datetime.now(timezone.utc).isoformat(),
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


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        logging.info("Noch keine state.json gefunden.")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OatlyCheckError(f"state.json konnte nicht gelesen werden: {exc}") from exc


def save_state(path: Path, snapshot: OfferSnapshot) -> None:
    try:
        path.write_text(
            json.dumps(asdict(snapshot), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise OatlyCheckError(f"state.json konnte nicht geschrieben werden: {exc}") from exc


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


def build_message(snapshot: OfferSnapshot, changes: list[str], threshold: Decimal | None) -> str:
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
        f"Haendler: {merchants}",
        f"Quelle: {snapshot.page_url}",
    ]

    if threshold is not None:
        message_lines.append(f"Preisgrenze: {format_decimal(threshold)} EUR")

    return "\n".join(message_lines)


def send_telegram_message(bot_token: str, chat_id: str, message: str) -> None:
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }

    logging.info("Sende Telegram-Nachricht.")
    try:
        response = requests.post(api_url, json=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise OatlyCheckError(f"Telegram-Nachricht konnte nicht gesendet werden: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise OatlyCheckError("Telegram API hat keine gueltige JSON-Antwort geliefert.") from exc

    if not data.get("ok"):
        raise OatlyCheckError(f"Telegram API meldet Fehler: {data}")


def main() -> int:
    configure_logging()

    threshold = load_price_threshold()
    bot_token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    previous_state = load_state(STATE_FILE)
    html = fetch_page(PAGE_URL)
    current_snapshot = parse_snapshot(html)
    changes = determine_changes(previous_state, current_snapshot)

    if should_notify(changes, current_snapshot, threshold):
        message = build_message(current_snapshot, changes, threshold)
        send_telegram_message(bot_token, chat_id, message)
        logging.info("Benachrichtigung gesendet.")
    else:
        logging.info("Keine Benachrichtigung erforderlich.")

    save_state(STATE_FILE, current_snapshot)
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
