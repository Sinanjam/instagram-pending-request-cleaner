#!/usr/bin/env python3
"""Safely review and withdraw pending Instagram follow requests from an export.

Selenium yalnızca görünen Instagram web arayüzüyle etkileşir; giriş şifresini okumaz.
Web arayüzü değişirse güvenli tarafta kalarak bilinmeyen düğmelere basmaz.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from collections import Counter
import hashlib
import io
import json
import fcntl
import os
import random
import re
import shutil
import sys
import time
import traceback
import unicodedata
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

APP_NAME = "instagram-request-cleaner"


def default_state_dir() -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    return (Path(root).expanduser() if root else Path.home() / ".local" / "state") / APP_NAME


STATE_DIR = default_state_dir()
PROFILE_DIR = STATE_DIR / "firefox-profile"
PROGRESS_PATH = STATE_DIR / "progress.json"
DEFERRED_PATH = STATE_DIR / "deferred.json"
AUDIT_PATH = STATE_DIR / "actions.jsonl"
MODAL_TRACE_PATH = STATE_DIR / "last-modal-timeline.json"
MODAL_DIAGNOSTIC_PATH = STATE_DIR / "last-modal-diagnostic.json"
UI_STATE_PATH = STATE_DIR / "last-ui-state.json"
SCREENSHOT_PATH = STATE_DIR / "last-click-error.png"
LOCK_PATH = STATE_DIR / "overnight.lock"
DEFERRED_REVIEW_PATH = STATE_DIR / "deferred-review.json"
DEFERRED_REVIEW_CSV_PATH = STATE_DIR / "deferred-review-report.csv"
DEFERRED_REVIEW_MD_PATH = STATE_DIR / "deferred-review-report.md"
DEFERRED_ALLOWLIST_PATH = STATE_DIR / "deferred-apply-allowlist.json"
EXIT_OK = 0
EXIT_TRANSIENT = 1
EXIT_DATA_ERROR = 2
EXIT_SAFETY_STOP = 3
SELENIUM_FIREFOX_APP_ID = "ig-request-cleaner-firefox"
DEFERRED_CLASSIFICATIONS = {
    "active_requested", "active_not_pending", "active_following",
    "unavailable_unknown", "possibly_deactivated", "deactivated_confirmed",
    "possibly_deleted_or_renamed", "temporarily_unavailable",
    "verification_required",
}


def configure_runtime_paths(state_dir: Path, profile_dir: Path | None = None) -> None:
    """Configure writable runtime paths outside the package/Nix store."""
    global STATE_DIR, PROFILE_DIR, PROGRESS_PATH, DEFERRED_PATH, AUDIT_PATH
    global MODAL_TRACE_PATH, MODAL_DIAGNOSTIC_PATH, UI_STATE_PATH, SCREENSHOT_PATH
    global LOCK_PATH, DEFERRED_REVIEW_PATH, DEFERRED_REVIEW_CSV_PATH
    global DEFERRED_REVIEW_MD_PATH, DEFERRED_ALLOWLIST_PATH
    STATE_DIR = state_dir.expanduser().resolve()
    PROFILE_DIR = (profile_dir.expanduser().resolve() if profile_dir else
                   STATE_DIR / "firefox-profile")
    PROGRESS_PATH = STATE_DIR / "progress.json"
    DEFERRED_PATH = STATE_DIR / "deferred.json"
    AUDIT_PATH = STATE_DIR / "actions.jsonl"
    MODAL_TRACE_PATH = STATE_DIR / "last-modal-timeline.json"
    MODAL_DIAGNOSTIC_PATH = STATE_DIR / "last-modal-diagnostic.json"
    UI_STATE_PATH = STATE_DIR / "last-ui-state.json"
    SCREENSHOT_PATH = STATE_DIR / "last-click-error.png"
    LOCK_PATH = STATE_DIR / "run.lock"
    DEFERRED_REVIEW_PATH = STATE_DIR / "deferred-review.json"
    DEFERRED_REVIEW_CSV_PATH = STATE_DIR / "deferred-review-report.csv"
    DEFERRED_REVIEW_MD_PATH = STATE_DIR / "deferred-review-report.md"
    DEFERRED_ALLOWLIST_PATH = STATE_DIR / "deferred-apply-allowlist.json"


def run_exit_code(safety_stop: bool, transient_failure: bool, failures: int) -> int:
    if safety_stop:
        return EXIT_SAFETY_STOP
    if transient_failure or failures:
        return EXIT_TRANSIENT
    return EXIT_OK


def log_unexpected_operational_error(name: str, exc: Exception) -> None:
    """Audit a non-safety failure and expose its traceback to systemd stderr."""
    exception_type = type(exc).__name__
    audit_event(name, "failed", f"unexpected_operational_error; exception={exception_type}")
    print(f"OPERASYONEL HATA: {exception_type}: {exc}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)


class AmbiguousCancellationError(RuntimeError):
    """A cancellation button was pressed, but the final outcome is unknown."""


class EmptyConfirmationDialogError(AmbiguousCancellationError):
    """A follow-request click produced a visible but empty Instagram dialog.

    This can indicate an Instagram page failure or a temporary restriction.
    Do not move on to another account in the same session.
    """


class AmbiguousConfirmationDialogError(RuntimeError):
    """No confirmation was clicked because the safe dialog/control was ambiguous."""


class RenderingTimeoutBeforeConfirmationClick(AmbiguousConfirmationDialogError):
    """A verified rendering state timed out before any confirmation click."""

    confirmation_clicked = False

    def __init__(self, message: str, timeout_observation: dict | None = None):
        super().__init__(message)
        self.timeout_observation = timeout_observation


class ProfileUnavailableError(RuntimeError):
    """Profile page explicitly says it is unavailable; no follow button was clicked."""
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(ch for ch in value if not unicodedata.combining(ch)).split())


def valid_username(value) -> str | None:
    if isinstance(value, str):
        value = value.strip().lstrip("@")
        if USERNAME_RE.fullmatch(value):
            return value
    return None


def username_from_instagram_url(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        url = urlsplit(value.strip())
    except ValueError:
        return None
    if url.scheme not in {"http", "https"} or (url.hostname or "").lower() not in {
        "instagram.com", "www.instagram.com", "m.instagram.com"
    }:
        return None
    parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
    if len(parts) == 2 and parts[0] == "_u":
        return valid_username(parts[1])
    if len(parts) != 1 or parts[0].lower() in {
        "accounts", "about", "challenge", "direct", "explore", "p", "reel",
        "reels", "stories", "privacy", "terms", "help", "developer", "web"
    }:
        return None
    return valid_username(parts[0])


def recover_mojibake(value: str) -> str:
    """Bazen Meta'nın UTF-8 etiketleri Latin-1/CP1252 olarak bozuluyor.

    Kişiye ait 'value' içeriğine dokunulmaz; sadece *label* düzeltilir.
    """
    if not isinstance(value, str):
        return ""
    for encoding in ("latin-1", "cp1252"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
        except (UnicodeError, ValueError):
            continue
        # Sadece kuvvetli mojibake işaretleri varsa düzelt.
        if any(marker in value for marker in ("Ã", "Ä", "Å", "Â", "â€", "ì", "ë", "ð")):
            return repaired
    return value


def is_username_label(label) -> bool:
    if not isinstance(label, str):
        return False
    normalized = normalize(recover_mojibake(label)).replace("ı", "i").replace("_", " ")
    return any(tag in normalized for tag in (
        "username", "user name", "kullanici adi", "kullanici ismi",
        "hesap adi", "instagram kullanicisi", "handle",
        "nombre de usuario", "nome de usuario", "nome utente",
        "nom d'utilisateur", "benutzername", "nazwa uzytkownika",
        "사용자 이름", "사용자이름", "ユーザーネーム", "اسم المستخدم",
        "имя пользователя"
    ))


def field_type(value) -> str:
    """Tanılama: asla kullanıcı adı, URL veya başka kişisel değer yazma."""
    if isinstance(value, str):
        if username_from_instagram_url(value):
            return "instagram-profil-url"
        if value.startswith(("http://", "https://")):
            return "diger-url"
        if valid_username(value):
            return "kullanici-adina-benziyor"
        return "metin"
    return type(value).__name__


def print_diagnostics(entries: list) -> None:
    """Yalnızca JSON şemasını yazdırır; kişisel değer içermez."""
    pending = [e for e in entries if isinstance(e, dict) and not username_from_entry(e)]
    shapes = Counter()
    examples = {}
    for entry in pending:
        labels = entry.get("label_values")
        if isinstance(labels, list):
            # Etiket yerine bazen hesap adı gelebileceği için ham etiketi bile yazdırmıyoruz.
            shape = tuple((
                "username-label" if is_username_label(item.get("label")) else "other-label",
                field_type(item.get("value"))
            ) if isinstance(item, dict) else (type(item).__name__, "") for item in labels)
        else:
            shape = (("no-label-values", type(labels).__name__),)
        shapes[shape] += 1
        examples.setdefault(shape, tuple(sorted(entry.keys())))
    print("\nTANILAMA (kişisel veri basılmıyor):")
    print(f"  Çözümlenemeyen kayıt: {len(pending)}")
    for shape, count in shapes.most_common(4):
        print(f"  {count} kayıt: alanlar={list(examples[shape])}, label_values={list(shape)}")
    print("  Bu çıktıyı paylaşabilirsin; kullanıcı adları ve URL içerikleri gizli tutuldu.")


def username_from_entry(entry: dict) -> str | None:
    # Eski Meta arşivleri: string_list_data içindeki value / href.
    values = entry.get("string_list_data")
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, dict):
                continue
            username = valid_username(item.get("value"))
            if username:
                return username
            username = username_from_instagram_url(item.get("href"))
            if username:
                return username

    # Yeni Meta arşivleri: label_values içinde diline göre adlandırılmış alanlar.
    # Kullanıcı adı alanı, görünen isimden (display name) ayrılmalı.
    labels = entry.get("label_values")
    if isinstance(labels, list):
        for item in labels:
            if isinstance(item, dict) and is_username_label(item.get("label")):
                username = valid_username(item.get("value"))
                if username:
                    return username
        # Yerelleştirilmiş veya bozulmuş etiketlerde de Instagram profil URL'si iş görür.
        for item in labels:
            if isinstance(item, dict):
                for key in ("value", "href"):
                    username = username_from_instagram_url(item.get(key))
                    if username:
                        return username
        if labels:
            # Yeni biçimdeki title görünen ad olabilir; belirsizse hesabı atla.
            return None

    # title, eski bazı dışa aktarımlarda doğrudan kullanıcı adıdır.
    return valid_username(entry.get("title"))


def usernames_from_json(raw: bytes, diagnose: bool = False) -> list[str]:
    data = json.loads(raw.decode("utf-8-sig"))
    if isinstance(data, dict):
        entries = data.get("relationships_follow_requests_sent")
    elif isinstance(data, list):
        entries = data
    else:
        entries = None
    if not isinstance(entries, list):
        raise ValueError("JSON'da 'relationships_follow_requests_sent' listesi yok. Yanlış dışa aktarım dosyası olabilir.")

    names = []
    seen = set()
    unresolved = 0
    for entry in entries:
        if not isinstance(entry, dict):
            unresolved += 1
            continue
        name = username_from_entry(entry)
        if not name:
            unresolved += 1
        elif name.casefold() not in seen:
            names.append(name)
            seen.add(name.casefold())

    # Veri doluysa fakat hiçbir kullanıcı adı bulunamadıysa sessizce 0 gösterme.
    if entries and not names:
        first = next((e for e in entries if isinstance(e, dict)), {})
        keys = ", ".join(str(k) for k in first.keys()) or "bulunamadı"
        label_list = first.get("label_values", [])
        labels = (
            ", ".join(str(i.get("label", "")) for i in label_list[:8] if isinstance(i, dict))
            if isinstance(label_list, list) else ""
        )
        raise ValueError(
            f"Dosyada {len(entries)} kayıt var fakat kullanıcı adı çözümlenemedi. "
            f"İlk kayıt alanları: {keys}. Etiketler: {labels or 'yok'}. "
            "Bu bilgileri (kullanıcı adı paylaşmadan) hata raporu olarak gönder."
        )
    if unresolved:
        print(
            f"UYARI: {len(entries)} kaydın {unresolved} tanesinde kullanıcı adı "
            "güvenle belirlenemedi; bunlara dokunulmayacak.",
            file=sys.stderr,
        )
    if diagnose and unresolved:
        print_diagnostics(entries)
    return names


def read_export(path: Path, diagnose: bool = False) -> tuple[list[str], str]:
    if path.is_file() and path.suffix.lower() == ".json":
        raw = path.read_bytes()
    elif path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            matches = [p for p in archive.namelist() if Path(p).name == "pending_follow_requests.json"]
            if len(matches) != 1:
                raise ValueError(f"ZIP'te tam bir 'pending_follow_requests.json' bulunmalı. Bulunan: {len(matches)}")
            raw = archive.read(matches[0])
    elif path.is_dir():
        matches = list(path.rglob("pending_follow_requests.json"))
        if len(matches) != 1:
            raise ValueError(f"Klasörde tam bir 'pending_follow_requests.json' bulunmalı. Bulunan: {len(matches)}")
        raw = matches[0].read_bytes()
    else:
        raise ValueError("Girdi olarak Instagram ZIP arşivini, JSON dosyasını veya açılmış arşiv klasörünü ver.")
    names = usernames_from_json(raw, diagnose=diagnose)
    return names, hashlib.sha256(raw).hexdigest()


def load_progress(source_hash: str) -> dict[str, str]:
    if not PROGRESS_PATH.exists():
        return {}
    try:
        data = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        if data.get("source_sha256") == source_hash and isinstance(data.get("processed"), dict):
            return dict(data["processed"])
    except (OSError, ValueError):
        pass
    return {}


def save_progress(source_hash: str, processed: dict[str, str]) -> None:
    data = {"source_sha256": source_hash, "processed": processed}
    tmp = PROGRESS_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False, indent=2))
        stream.flush()
        import os
        os.fsync(stream.fileno())
    tmp.replace(PROGRESS_PATH)
    dirfd = os.open(PROGRESS_PATH.parent, os.O_DIRECTORY)
    try: os.fsync(dirfd)
    finally: os.close(dirfd)



def load_deferred(source_hash: str) -> dict[str, dict]:
    if not DEFERRED_PATH.exists():
        return {}
    try:
        data = json.loads(DEFERRED_PATH.read_text(encoding="utf-8"))
        if data.get("source_sha256") == source_hash and isinstance(data.get("accounts"), dict):
            return {
                name.casefold(): record
                for name, record in data["accounts"].items()
                if isinstance(record, dict)
            }
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def save_deferred(source_hash: str, deferred: dict[str, dict]) -> None:
    data = {"source_sha256": source_hash, "accounts": deferred}
    temp = DEFERRED_PATH.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False, indent=2))
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temp.replace(DEFERRED_PATH)
    dirfd = os.open(DEFERRED_PATH.parent, os.O_DIRECTORY)
    try: os.fsync(dirfd)
    finally: os.close(dirfd)


def validate_state_integrity(source_hash: str, usernames: set[str]) -> None:
    """Reject malformed, mismatched, unknown, or overlapping progress state."""
    processed = {}
    deferred = {}
    for path, field, target in ((PROGRESS_PATH, "processed", processed),
                                (DEFERRED_PATH, "accounts", deferred)):
        if not path.exists():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Durum kaydı okunamıyor ({path.name}); hiçbir işlem yapılmayacak.") from exc
        if not isinstance(doc, dict) or doc.get("source_sha256") != source_hash or not isinstance(doc.get(field), dict):
            raise RuntimeError(f"Durum kaydı arşivle uyuşmuyor veya biçimi bozuk ({path.name}); işlem durduruldu.")
        target.update(doc[field])
    known = {u.casefold() for u in usernames}
    if any(not isinstance(k, str) or k.casefold() not in known for k in processed):
        raise RuntimeError("İlerleme kaydında arşivde olmayan hesap var; işlem durduruldu.")
    if any(not isinstance(v, str) or v not in {"cancelled", "not_pending"}
           for v in processed.values()):
        raise RuntimeError("İlerleme kaydında bilinmeyen sonuç var; işlem durduruldu.")
    if any(not isinstance(k, str) or k.casefold() not in known or not isinstance(v, dict)
           for k, v in deferred.items()):
        raise RuntimeError("Ertelenenler kaydı bozuk veya arşivle uyuşmuyor; işlem durduruldu.")
    overlap = {k.casefold() for k in processed} & {k.casefold() for k in deferred}
    if overlap:
        raise RuntimeError("Bir hesap hem işlenmiş hem ertelenmiş görünüyor; kayıt bütünlüğü için duruldu.")


def audit_event(name: str, result: str, verification: str) -> None:
    """Append a minimal local audit record; never include page text or credentials."""
    record = {"time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "username": name, "result": result, "verification": verification}
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with AUDIT_PATH.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        import os
        os.fsync(stream.fileno())
    category = {
        "cancelled": "cancelled-actions.jsonl",
        "not_pending": "not-pending-actions.jsonl",
        "deferred": "deferred-actions.jsonl",
        "inaccessible": "inaccessible-actions.jsonl",
        "failed": "failures-actions.jsonl",
    }.get(result)
    if category:
        with (STATE_DIR / category).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def atomic_write_json(path: Path, value) -> None:
    """Durably replace a JSON diagnostic before recovery can continue."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def atomic_write_text(path: Path, value: str) -> None:
    """Durably replace a generated text report without exposing partial output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_deferred_review(source_hash: str, usernames: set[str]) -> dict[str, dict]:
    if not DEFERRED_REVIEW_PATH.exists():
        return {}
    try:
        document = json.loads(DEFERRED_REVIEW_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Deferred inceleme kaydı okunamıyor; hiçbir işlem yapılmayacak.") from exc
    accounts = document.get("accounts") if isinstance(document, dict) else None
    if (not isinstance(document, dict) or document.get("source_sha256") != source_hash
            or not isinstance(accounts, dict)):
        raise RuntimeError("Deferred inceleme kaydı arşivle uyuşmuyor veya biçimi bozuk.")
    known = {name.casefold() for name in usernames}
    result = {}
    for name, record in accounts.items():
        if (not isinstance(name, str) or name.casefold() not in known
                or not isinstance(record, dict)
                or record.get("classification") not in DEFERRED_CLASSIFICATIONS):
            raise RuntimeError("Deferred inceleme kaydında bilinmeyen hesap veya sınıflandırma var.")
        result[name.casefold()] = dict(record)
    return result


def save_deferred_review(source_hash: str, accounts: dict[str, dict]) -> None:
    atomic_write_json(DEFERRED_REVIEW_PATH, {
        "source_sha256": source_hash,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "accounts": accounts,
    })


def deferred_review_summary(deferred: dict[str, dict], reviews: dict[str, dict]) -> dict:
    current = set(deferred)
    reviewed_current = {name: reviews[name] for name in current if name in reviews}
    counts = Counter(record.get("classification") for record in reviewed_current.values())
    all_counts = Counter(record.get("classification") for record in reviews.values())
    resolved_cancelled = sum(
        record.get("resolution") == "cancelled" for record in reviews.values()
    )
    resolved_not_pending = sum(
        record.get("resolution") == "not_pending" for record in reviews.values()
    )
    manual_classes = {
        "unavailable_unknown", "possibly_deactivated",
        "possibly_deleted_or_renamed", "verification_required",
    }
    return {
        "deferred_total": len(current),
        "review_records": len(reviews),
        "reviewed": len(reviewed_current),
        "unreviewed": len(current - set(reviewed_current)),
        "classifications": {name: counts.get(name, 0)
                            for name in sorted(DEFERRED_CLASSIFICATIONS)},
        "all_classifications": {name: all_counts.get(name, 0)
                                for name in sorted(DEFERRED_CLASSIFICATIONS)},
        "active_total": sum(counts.get(name, 0) for name in (
            "active_requested", "active_not_pending", "active_following")),
        "resolved_cancelled": resolved_cancelled,
        "resolved_not_pending": resolved_not_pending,
        "manual_review": sum(
            record.get("classification") in manual_classes
            or record.get("manual_review_required") is True
            for record in reviewed_current.values()
        ),
    }


def write_deferred_reports(source_hash: str, deferred: dict[str, dict],
                           reviews: dict[str, dict]) -> dict:
    """Generate sanitized JSON/CSV/Markdown views; never include page text or tokens."""
    save_deferred_review(source_hash, reviews)
    summary = deferred_review_summary(deferred, reviews)
    columns = [
        "username", "old_reason", "classification", "evidence_type",
        "checked_at_utc", "confidence", "manual_review_required",
        "apply_excluded_reason", "resolution", "resolved_at_utc",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for name in sorted(reviews):
        record = reviews[name]
        writer.writerow({column: (name if column == "username" else record.get(column, ""))
                         for column in columns})
    atomic_write_text(DEFERRED_REVIEW_CSV_PATH, output.getvalue())

    counts = summary["all_classifications"]
    lines = [
        "# Deferred Instagram Hesap İncelemesi", "",
        f"- Güncelleme (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Mevcut ertelenen: {summary['deferred_total']}",
        f"- Toplam inceleme kaydı: {summary['review_records']}",
        f"- Mevcut ertelenenlerden incelenen: {summary['reviewed']}",
        f"- İncelenmemiş: {summary['unreviewed']}",
        f"- Güvenle iptal edilen: {summary['resolved_cancelled']}",
        f"- Manuel kontrol gereken: {summary['manual_review']}", "",
        "## Sınıflandırmalar (tüm inceleme kayıtları)", "",
        "| Sınıflandırma | Sayı |", "|---|---:|",
    ]
    lines.extend(f"| `{name}` | {counts[name]} |" for name in sorted(counts))
    suspicious = [
        name for name, record in sorted(reviews.items())
        if name in deferred and record.get("classification") in {
            "possibly_deactivated", "deactivated_confirmed"
        }
    ]
    manual = [
        name for name, record in sorted(reviews.items())
        if name in deferred and (
            record.get("manual_review_required") is True
            or record.get("classification") in {
                "unavailable_unknown", "possibly_deactivated",
                "possibly_deleted_or_renamed", "verification_required"
            }
        )
    ]
    lines.extend(["", "## Deactivation kanıtı veya şüphesi", ""])
    lines.extend([f"- `{name}`" for name in suspicious] or ["- Yok"])
    lines.extend(["", "## Manuel kontrol gerekenler", ""])
    lines.extend([f"- `{name}`" for name in manual] or ["- Yok"])
    lines.extend(["", "_‘Sorry, this page isn't available’ tek başına deactivation veya silinme kanıtı sayılmaz._", ""])
    atomic_write_text(DEFERRED_REVIEW_MD_PATH, "\n".join(lines))
    return summary


def deferred_review_record(old_reason: str, classification: str, evidence_type: str,
                           confidence: str) -> dict:
    if classification not in DEFERRED_CLASSIFICATIONS:
        raise ValueError("Bilinmeyen deferred sınıflandırması")
    return {
        "old_reason": old_reason,
        "classification": classification,
        "evidence_type": evidence_type,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "confidence": confidence,
    }


def visible_profile_page_text(driver) -> str:
    """Read a small visible slice only for exact status phrases; never persist it."""
    from selenium.webdriver.common.by import By
    body = driver.find_elements(By.TAG_NAME, "body")
    return normalize(body[0].text[:2500]).replace("ı", "i") if body else ""


def classify_deferred_profile(driver, name: str, old_reason: str,
                              timeout: float = 20.0, poll_seconds: float = 0.5) -> dict:
    """Classify one deferred profile without clicking any profile control."""
    open_profile_fresh(driver, name)
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        guard_session(driver)
        state = profile_state(driver)
        if state == "requested":
            return deferred_review_record(
                old_reason, "active_requested", "visible_requested_control", "high")
        if state == "follow":
            return deferred_review_record(
                old_reason, "active_not_pending", "visible_follow_control", "high")
        if state == "following":
            return deferred_review_record(
                old_reason, "active_following", "visible_following_control", "high")

        location = urlsplit(driver.current_url)
        if location.scheme == "about" and location.path.startswith("neterror"):
            return deferred_review_record(
                old_reason, "temporarily_unavailable", "browser_network_error", "high")
        text = visible_profile_page_text(driver)
        if any(phrase in text for phrase in (
            "this account has been deactivated", "account is deactivated",
            "bu hesap donduruldu", "bu hesap devre disi birakildi",
        )):
            return deferred_review_record(
                old_reason, "deactivated_confirmed",
                "explicit_instagram_deactivated_message", "high")
        if is_unavailable_profile_page(driver, name):
            return deferred_review_record(
                old_reason, "unavailable_unknown",
                "explicit_page_unavailable_message", "high")
        if "user not found" in text or "kullanici bulunamadi" in text:
            return deferred_review_record(
                old_reason, "possibly_deleted_or_renamed",
                "explicit_user_not_found_message", "medium")
        time.sleep(poll_seconds)
    guard_session(driver)
    return deferred_review_record(
        old_reason, "verification_required", "profile_state_unrecognized", "low")


def defer_user(deferred: dict[str, dict], name: str, reason: str) -> None:
    # No password, cookies, arbitrary page text or personal metadata is stored.
    deferred[name.casefold()] = {
        "reason": reason,
        "noted_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def element_label(element) -> str:
    return normalize(element.text or element.get_attribute("aria-label") or "")


def is_requested(label: str) -> bool:
    return label in {
        "requested", "request sent", "follow request sent", "request pending",
        "istek gonderildi", "takip istegi gonderildi",
    }


def is_follow(label: str) -> bool:
    return label in {"follow", "takip et"}


def is_following(label: str) -> bool:
    return label in {"following", "takiptesin", "takip ediliyor"}


def find_profile_button(driver, test):
    from selenium.webdriver.common.by import By

    # Yalnızca profil başlığını tarar, önerilerdeki "Takip et" düğmelerine dokunmaz.
    xpath = "//main//header//button | //main//header//*[@role='button'] | //header//button | //header//*[@role='button']"
    for element in driver.find_elements(By.XPATH, xpath):
        try:
            if element.is_displayed() and element.is_enabled() and test(element_label(element)):
                return element
        except Exception:
            continue
    return None


def is_request_withdrawal_dialog_text(text: str, name: str) -> bool:
    """Onay cümlesi yalnızca hedef hesaba yeniden istek göndermeyi anlatmalı.

    Unfollow tek başına yeterli DEĞİLDİR: halihazırda takip edilen bir hesabı
    yanlışlıkla takipten çıkarmamak için kullanıcı adı + açıklama eşleşir.
    """
    t = normalize(text).replace("ı", "i")
    account = f"@{name.casefold()}"
    if account not in t:
        return False
    english = "request to follow" in t and "again" in t
    turkish = (
        ("takip isteg" in t or "takip taleb" in t)
        and ("yeniden" in t or "tekrar" in t)
    )
    return english or turkish


ATOMIC_CONFIRMATION_OBSERVATION_JS = r"""
const target = String(arguments[0] || '').toLowerCase();
const norm = s => (s || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
  .toLocaleLowerCase().replace(/ı/g, 'i').replace(/\s+/g, ' ').trim();
const round = n => Math.round(Number(n) * 100) / 100;
const nodeShape = e => e ? {tag:(e.tagName || '').toLowerCase(), role:e.getAttribute?.('role') || null,
  ariaHidden:e.getAttribute?.('aria-hidden') || null, inert:!!e.inert} : null;
const inspectVisibility = e => {
  if (!e) return {visible:false, reasons:['missing'], connected:false, ancestors:[], domPath:[]};
  const r=e.getBoundingClientRect(), s=getComputedStyle(e), rects=[...e.getClientRects()].slice(0,4)
    .map(x=>({x:round(x.x),y:round(x.y),width:round(x.width),height:round(x.height)}));
  const rect={x:round(r.x),y:round(r.y),top:round(r.top),right:round(r.right),
    bottom:round(r.bottom),left:round(r.left),width:round(r.width),height:round(r.height)};
  const reasons=[];
  if(!e.isConnected)reasons.push('disconnected'); if(e.hidden)reasons.push('hidden');
  if(e.getAttribute('aria-hidden')==='true')reasons.push('aria-hidden'); if(e.inert)reasons.push('inert');
  if(!(r.width>0&&r.height>0))reasons.push('empty-rect'); if(rects.length===0)reasons.push('no-client-rects');
  if(s.display==='none')reasons.push('display-none');
  if(s.visibility==='hidden'||s.visibility==='collapse')reasons.push('visibility-hidden');
  if(Number(s.opacity)<=0)reasons.push('opacity-zero');
  const ancestors=[];
  for(let p=e.parentElement,depth=1;p&&depth<=16;p=p.parentElement,depth++){
    const ps=getComputedStyle(p), pr=p.getBoundingClientRect(), blocking=[];
    if(p.hidden)blocking.push('hidden'); if(p.getAttribute('aria-hidden')==='true')blocking.push('aria-hidden');
    if(p.inert)blocking.push('inert'); if(ps.display==='none')blocking.push('display-none');
    if(ps.visibility==='hidden'||ps.visibility==='collapse')blocking.push('visibility-hidden');
    if(Number(ps.opacity)<=0)blocking.push('opacity-zero');
    ancestors.push({...nodeShape(p),depth,display:ps.display,visibility:ps.visibility,
      opacity:ps.opacity,pointerEvents:ps.pointerEvents,
      rect:{x:round(pr.x),y:round(pr.y),width:round(pr.width),height:round(pr.height)},blocking});
    for(const reason of blocking)if(!reasons.includes('ancestor-'+reason))reasons.push('ancestor-'+reason);
  }
  const inViewport=r.right>0&&r.bottom>0&&r.left<innerWidth&&r.top<innerHeight;
  const fullyInViewport=r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;
  const cx=Math.max(0,Math.min(innerWidth-1,r.left+r.width/2));
  const cy=Math.max(0,Math.min(innerHeight-1,r.top+r.height/2));
  const centerUsable=r.width>0&&r.height>0&&inViewport&&cx>=r.left&&cx<r.right&&cy>=r.top&&cy<r.bottom;
  const top=centerUsable?document.elementFromPoint(cx,cy):null;
  const topMatches=!!top&&(top===e||e.contains(top));
  const domPath=[];
  for(let p=e,depth=0;p&&depth<=12;p=p.parentElement,depth++){
    domPath.push({...nodeShape(p),depth});
    if(p!==e&&(p.matches('[role="dialog"],[role="alertdialog"],[role="menu"],[aria-modal="true"]')))break;
  }
  return {visible:reasons.length===0,connected:!!e.isConnected,hidden:!!e.hidden,
    ariaHidden:e.getAttribute('aria-hidden'),inert:!!e.inert,display:s.display,
    visibility:s.visibility,opacity:s.opacity,pointerEvents:s.pointerEvents,rect,clientRects:rects,
    clientRectCount:e.getClientRects().length,inViewport,fullyInViewport,
    center:{x:round(cx),y:round(cy),usable:centerUsable,top:nodeShape(top),topMatches},
    covered:centerUsable&&!topMatches,reasons,ancestors,domPath};
};
window.__igSafeVisibilityDetails=inspectVisibility;
const visible = e => inspectVisibility(e).visible;
const enabled = e => !e.disabled && e.getAttribute('aria-disabled') !== 'true';
const label = e => norm(e.innerText || e.textContent || e.getAttribute('aria-label') || '');
const messageMatch = text => {
  const t = norm(text), account = '@' + target;
  return t.includes(account) && ((t.includes('request to follow') && t.includes('again')) ||
    ((t.includes('takip istegi') || t.includes('takip taleb')) &&
      (t.includes('yeniden') || t.includes('tekrar'))));
};
const clearLabels = new Set(['cancel request','cancel follow request','withdraw request',
  'withdraw follow request','unsend request','cancel pending request','istegi iptal et',
  'takip istegini iptal et','gonderilen istegi iptal et','istegi geri cek',
  'takip istegini geri cek','takip istegini geri al']);
const unfollowLabels = new Set(['unfollow','takibi birak']);
const dismissLabels = new Set(['cancel','iptal']);
const hostOK = ['instagram.com','www.instagram.com','m.instagram.com'].includes(location.hostname.toLowerCase());
const pathOK = decodeURIComponent(location.pathname).replace(/^\/+|\/+$/g, '').toLowerCase() === target;
const nodes = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"],[role="menu"],[aria-modal="true"]')];
// Role verilmeyen Instagram varyantında yalnızca güvenli metinli Unfollow düğmesinden
// başlayıp en küçük ortak atayı ekle. Body/main/header hiçbir zaman aday değildir.
for (const action of document.querySelectorAll('button,[role="button"]')) {
  if (!visible(action) || !enabled(action) || !unfollowLabels.has(label(action))) continue;
  // Açık role sahip bir popup içindeki aynı düğmeyi rolsüz yedek üzerinden
  // ikinci kez adaylaştırma. Bu, tek kontrolü iki olası kontrol gibi gösterir.
  if (nodes.some(node => node === action || node.contains(action))) continue;
  let p = action.parentElement;
  for (let depth = 1; p && depth <= 8; depth++, p = p.parentElement) {
    if (['BODY','HTML','MAIN','NAV','HEADER'].includes(p.tagName)) break;
    const text = p.innerText || p.textContent || '';
    if (norm(text).length > 1200) break;
    if (visible(p) && messageMatch(text)) { if (!nodes.includes(p)) nodes.push(p); break; }
  }
}
const dialogs = nodes.map((node, index) => {
  const text = node.innerText || node.textContent || '';
  const controls = [...node.querySelectorAll('button,[role="button"],[role="menuitem"]')].map(control => {
    const visibilityDetails=inspectVisibility(control);
    return {element:control,label:label(control),visible:visibilityDetails.visible,
      enabled:enabled(control),visibilityDetails};
  });
  const actionable = controls.filter(c => c.visible && c.enabled &&
    c.visibilityDetails.inViewport && c.visibilityDetails.center.topMatches &&
    !c.visibilityDetails.covered);
  const clear = actionable.filter(c => clearLabels.has(c.label));
  const unfollow = actionable.filter(c => unfollowLabels.has(c.label));
  const dismiss = actionable.filter(c => dismissLabels.has(c.label));
  const confirmationControls = [...clear, ...unfollow];
  const targetMessage = messageMatch(text);
  const safe = visible(node) && targetMessage &&
    confirmationControls.length === 1 &&
    ((clear.length === 1) || (unfollow.length === 1 && dismiss.length === 1));
  const structural = controls.filter(c => c.enabled);
  const structuralClear = structural.filter(c => clearLabels.has(c.label));
  const structuralUnfollow = structural.filter(c => unfollowLabels.has(c.label));
  const structuralDismiss = structural.filter(c => dismissLabels.has(c.label));
  const structuralConfirmation = [...structuralClear, ...structuralUnfollow];
  const relevantStructural = [...structuralConfirmation, ...structuralDismiss];
  // Instagram can commit the structural confirmation controls under an
  // opacity:0 child before the withdrawal explanation finishes rendering.
  // Waiting is read-only: the final valid state still requires targetMessage.
  // A mention of any other account is an identity conflict and must not wait.
  const mentionedAccounts = [...norm(text).matchAll(/@([a-z0-9._]{1,30})/g)]
    .map(match => match[1]);
  const identityCompatible = mentionedAccounts.every(account => account === target);
  const rendering = visible(node) && identityCompatible && structuralConfirmation.length === 1 &&
    ((structuralClear.length === 1) ||
      (structuralUnfollow.length === 1 && structuralDismiss.length === 1)) &&
    relevantStructural.length > 0 && relevantStructural.every(c => !c.visible &&
      c.visibilityDetails.reasons.length === 1 &&
      c.visibilityDetails.reasons[0] === 'ancestor-opacity-zero');
  const button = clear.length === 1 ? clear[0].element :
    (unfollow.length === 1 && dismiss.length === 1 ? unfollow[0].element : null);
  const r = node.getBoundingClientRect();
  const visibilityDetails=inspectVisibility(node);
  return {index, element: node, role: node.getAttribute('role'),
    ariaModal: node.getAttribute('aria-modal') === 'true', visible: visibilityDetails.visible,
    width: Math.round(r.width), height: Math.round(r.height), text,
    textPresent: !!norm(text), textLength: norm(text).length, targetMessage,
    visibilityDetails,controls,safe,rendering,button};
});
const safeDialogs = dialogs.filter(d => d.safe);
const renderingDialogs = dialogs.filter(d => d.rendering);
const uniqueButtons = [];
for (const d of safeDialogs) if (d.button && !uniqueButtons.includes(d.button)) uniqueButtons.push(d.button);
const visibleDialogs = dialogs.filter(d => d.visible);
const trulyEmpty = visibleDialogs.length > 0 && visibleDialogs.every(d =>
  !d.textPresent && !d.controls.some(c => c.visible));
return {capturedAtEpochMs:Date.now(),urlMatchesTarget: hostOK && pathOK, dialogs, visibleDialogCount: visibleDialogs.length,
  safeDialogCount: safeDialogs.length, safeButtonCount: uniqueButtons.length,
  renderingDialogCount:renderingDialogs.length,
  trulyEmpty, status: uniqueButtons.length === 1 && safeDialogs.length === 1 ? 'valid' :
    (renderingDialogs.length === 1 ? 'rendering' :
      (trulyEmpty ? 'empty' : (visibleDialogs.length ? 'ambiguous' : 'none'))),
  button: uniqueButtons.length === 1 && safeDialogs.length === 1 ? uniqueButtons[0] : null};
"""


def observe_confirmation_dom(driver, name: str) -> dict:
    """Atomically read modal text, visibility and controls without mutating the DOM."""
    try:
        result = driver.execute_script(ATOMIC_CONFIRMATION_OBSERVATION_JS, name)
        return result if isinstance(result, dict) else {"status": "unavailable", "dialogs": []}
    except Exception:
        return {"status": "unavailable", "dialogs": []}


def sanitize_visibility_details(value) -> dict:
    """Keep only structural/CSS evidence; discard DOM elements and page text."""
    value = value if isinstance(value, dict) else {}
    allowed_reasons = {
        "missing", "disconnected", "hidden", "aria-hidden", "inert", "empty-rect",
        "no-client-rects", "display-none", "visibility-hidden", "opacity-zero",
        "ancestor-hidden", "ancestor-aria-hidden", "ancestor-inert",
        "ancestor-display-none", "ancestor-visibility-hidden", "ancestor-opacity-zero",
    }
    def number(raw):
        try:
            return round(max(-100000.0, min(100000.0, float(raw))), 2)
        except (TypeError, ValueError):
            return 0.0
    def css(raw):
        return raw[:40] if isinstance(raw, str) and re.fullmatch(r"[-a-zA-Z0-9. ()%,]+", raw[:40]) else "other"
    def shape(raw):
        raw = raw if isinstance(raw, dict) else {}
        tag = raw.get("tag")
        role = raw.get("role")
        return {"tag": tag if isinstance(tag, str) and re.fullmatch(r"[a-z0-9-]{1,24}", tag) else "other",
                "role": role if role in {None, "dialog", "alertdialog", "menu", "button", "menuitem"} else "other",
                "aria_hidden": raw.get("ariaHidden") if raw.get("ariaHidden") in {None, "true", "false"} else "other",
                "inert": bool(raw.get("inert"))}
    def rect(raw):
        raw = raw if isinstance(raw, dict) else {}
        return {key: number(raw.get(key)) for key in
                ("x", "y", "top", "right", "bottom", "left", "width", "height") if key in raw}
    center = value.get("center") if isinstance(value.get("center"), dict) else {}
    result = {
        "visible": bool(value.get("visible")), "connected": bool(value.get("connected")),
        "hidden": bool(value.get("hidden")),
        "aria_hidden": value.get("ariaHidden") if value.get("ariaHidden") in {None, "true", "false"} else "other",
        "inert": bool(value.get("inert")), "display": css(value.get("display")),
        "visibility": css(value.get("visibility")), "opacity": css(value.get("opacity")),
        "pointer_events": css(value.get("pointerEvents")), "rect": rect(value.get("rect")),
        "client_rect_count": max(0, min(100, int(value.get("clientRectCount", 0)))),
        "client_rects": [rect(x) for x in value.get("clientRects", [])[:4] if isinstance(x, dict)],
        "in_viewport": bool(value.get("inViewport")),
        "fully_in_viewport": bool(value.get("fullyInViewport")),
        "covered": bool(value.get("covered")),
        "center": {"x": number(center.get("x")), "y": number(center.get("y")),
                   "usable": bool(center.get("usable")), "top_matches": bool(center.get("topMatches")),
                   "top": shape(center.get("top")) if center.get("top") else None},
        "reasons": [x for x in value.get("reasons", []) if x in allowed_reasons],
        "ancestors": [], "dom_path": [],
    }
    for item in value.get("ancestors", [])[:16]:
        if not isinstance(item, dict):
            continue
        entry = shape(item)
        entry.update({"depth": max(1, min(16, int(item.get("depth", 1)))),
                      "display": css(item.get("display")),
                      "visibility": css(item.get("visibility")),
                      "opacity": css(item.get("opacity")),
                      "pointer_events": css(item.get("pointerEvents")),
                      "rect": rect(item.get("rect")),
                      "blocking": [x for x in item.get("blocking", [])
                                   if x in {"hidden", "aria-hidden", "inert", "display-none",
                                            "visibility-hidden", "opacity-zero"}]})
        result["ancestors"].append(entry)
    for item in value.get("domPath", [])[:13]:
        if isinstance(item, dict):
            entry = shape(item)
            entry["depth"] = max(0, min(12, int(item.get("depth", 0))))
            result["dom_path"].append(entry)
    return result


def find_confirmation(driver, name: str):
    """Bulunursa yalnızca hedef hesaba ait takip isteği iptal düğmesini döndürür.

    Bazı Instagram görünümleri `role=dialog` koymuyor. Bu durumda da ancak
    explanation says the target would need a new follow request,
    düğmeler bire bir Unfollow + Cancel ise, kapsayıcı gerçekten görünürse
    işlem yapar. Bunlar yoksa ASLA Unfollow'a basmaz.
    """
    from urllib.parse import urlsplit

    location = urlsplit(driver.current_url)
    if (location.hostname or "").lower() not in {
        "instagram.com", "www.instagram.com", "m.instagram.com"
    } or location.path.strip("/").casefold() != name.casefold():
        return None
    if profile_state(driver) != "requested":
        return None
    observation = observe_confirmation_dom(driver, name)
    if not observation.get("urlMatchesTarget") or observation.get("status") != "valid":
        return None
    return observation.get("button")


def wait_for_valid_confirmation(driver, name: str, timeout: float = 15.0,
                                poll_seconds: float = 0.25) -> None:
    """Wait only for a known opacity render transition; never retain an element.

    Every poll is a fresh atomic DOM read. Anything other than the short
    pre-dialog gap, ``rendering`` or ``valid`` fails closed immediately.
    """
    started = time.monotonic()
    first_poll = True
    while True:
        guard_session(driver)
        if profile_state(driver) != "requested":
            raise AmbiguousConfirmationDialogError(
                "Onay beklenirken hedef profil Requested durumundan belirsiz biçimde çıktı; "
                "onay düğmesine basılmadı."
            )
        observation = observe_confirmation_dom(driver, name)
        if not observation.get("urlMatchesTarget"):
            raise AmbiguousConfirmationDialogError(
                "Onay beklenirken hedef URL/hesap eşleşmesi kayboldu; onay düğmesine basılmadı."
            )
        status = observation.get("status")
        if status == "valid":
            # Deliberately return no WebElement. click_when_unobscured will call
            # find_confirmation again and obtain a fresh, revalidated element.
            return
        elapsed = time.monotonic() - started
        if status == "rendering":
            if elapsed >= timeout:
                raise RenderingTimeoutBeforeConfirmationClick(
                    f"Onay penceresi {timeout:g} saniyede görünür hale gelmedi; "
                    "onay düğmesine basılmadığı doğrulandı.",
                    timeout_observation=observation,
                )
        elif status == "none" and first_poll:
            # One poll permits the dialog node to be committed after Requested.
            pass
        elif status == "empty":
            raise EmptyConfirmationDialogError(
                "Requested tıklamasından sonra gerçekten boş onay penceresi oluştu; "
                "onay düğmesine basılmadı."
            )
        else:
            raise AmbiguousConfirmationDialogError(
                f"Onay penceresi güvenli biçimde seçilemedi (durum={status!r}); "
                "bekleyerek aşılmadı ve hiçbir onay düğmesine basılmadı."
            )
        if elapsed >= timeout:
            raise AmbiguousConfirmationDialogError(
                f"Onay penceresi {timeout:g} saniyede doğrulanamadı; onay düğmesine basılmadı."
            )
        first_poll = False
        time.sleep(min(0.5, max(0.25, poll_seconds)))


def safe_modal_diagnostics(driver, name: str, observation: dict | None = None,
                           screenshot_captured: bool = False) -> str:
    """Sadece UI durumunu kaydet; DOM/HTML, oturum belirteci veya çerez kaydetme."""
    try:
        observation = observation or observe_confirmation_dom(driver, name)
        captured_ms = observation.get("capturedAtEpochMs")
        try:
            captured_at = datetime.fromtimestamp(
                float(captured_ms) / 1000, timezone.utc).isoformat(timespec="milliseconds")
        except (TypeError, ValueError, OSError):
            captured_at = None
        info = {"profile_state": profile_state(driver), "requested_target": name,
                "observation_captured_at_utc": captured_at,
                "screenshot_captured_immediately_after_observation": screenshot_captured,
                "observation_status": observation.get("status"),
                "url_matches_target": bool(observation.get("urlMatchesTarget")),
                "visible_dialog_count": int(observation.get("visibleDialogCount", 0)),
                "safe_dialog_count": int(observation.get("safeDialogCount", 0)),
                "safe_button_count": int(observation.get("safeButtonCount", 0)),
                "rendering_dialog_count": int(observation.get("renderingDialogCount", 0)),
                "dialog_candidates": []}
        for modal in observation.get("dialogs", [])[:12]:
            controls = modal.get("controls", []) if isinstance(modal, dict) else []
            info["dialog_candidates"].append({
                "role": modal.get("role") or "(none)",
                "aria_modal": bool(modal.get("ariaModal")),
                "visible": bool(modal.get("visible")),
                "width": int(modal.get("width", 0)), "height": int(modal.get("height", 0)),
                "text_present": bool(modal.get("textPresent")),
                "text_length": int(modal.get("textLength", 0)),
                "matches_target_message": bool(modal.get("targetMessage")),
                "visibility_details": sanitize_visibility_details(modal.get("visibilityDetails")),
                "controls": [{
                    "label": c.get("label") if c.get("label") in {
                        "unfollow", "takibi birak", "cancel", "iptal", "cancel request",
                        "cancel follow request", "withdraw request", "withdraw follow request",
                        "unsend request", "cancel pending request", "istegi iptal et",
                        "takip istegini iptal et", "gonderilen istegi iptal et",
                        "istegi geri cek", "takip istegini geri cek", "takip istegini geri al"
                    } else "other",
                    "visible": bool(c.get("visible")), "enabled": bool(c.get("enabled")),
                    "visibility_details": sanitize_visibility_details(c.get("visibilityDetails")),
                } for c in controls[:12] if isinstance(c, dict)],
                "safe": bool(modal.get("safe")),
                "rendering": bool(modal.get("rendering")),
            })
        atomic_write_json(MODAL_DIAGNOSTIC_PATH, info)
        return f" Modal tanısı: {MODAL_DIAGNOSTIC_PATH}"
    except Exception:
        return ""


def save_atomic_modal_evidence(driver, name: str, observation: dict | None = None) -> str:
    """Pair one atomic DOM observation with the immediately following screenshot."""
    observation = observation or observe_confirmation_dom(driver, name)
    screenshot = save_debug_screenshot(driver)
    diagnostic = safe_modal_diagnostics(
        driver, name, observation=observation, screenshot_captured=bool(screenshot))
    return screenshot + diagnostic


def arm_modal_timeline(driver, name: str) -> bool:
    """Install passive, in-memory DOM/network/error summaries before the Requested click.

    Raw text, HTML, URLs, headers, cookies and request bodies are never returned
    from the page. Only allowlisted labels, boolean message matches and counts
    are retained. Open shadow roots and same-origin frames are included.
    """
    script = r"""
    const target = String(arguments[0]).toLowerCase();
    const trace = {started: performance.now(), changes: [], errors: 0, rejections: 0,
                   inaccessibleFrames: 0, openShadowRoots: 0, sameOriginFrames: 0};
    let seenRoots = new WeakSet();
    const observedRoots = new WeakSet();
    const allowed = new Set(['requested','request sent','unfollow','cancel','iptal',
      'takibi birak','istek gonderildi','takip et','follow']);
    const guardPhrases = {
      restriction: ['try again later','we restrict certain activity','please wait a few minutes',
        'islem engellendi','bir kac dakika sonra tekrar dene'],
      security_check: ['confirm it is you','security check','checkpoint','captcha',
        'unusual activity','guvenlik kontrolu','dogrulama gerekli']
    };
    const norm = s => (s || '').normalize('NFKD').replace(/[\u0300-\u036f]/g,'')
      .toLocaleLowerCase().replace(/ı/g,'i').replace(/\s+/g,' ').trim();
    const messageMatch = s => {
      const t=norm(s), account='@'+target;
      return t.includes(account) && ((t.includes('request to follow') && t.includes('again')) ||
        ((t.includes('takip istegi') || t.includes('takip taleb')) &&
          (t.includes('yeniden') || t.includes('tekrar'))));
    };
    let scopes=[];
    const details=e=>window.__igSafeVisibilityDetails?window.__igSafeVisibilityDetails(e):{};
    const visibility=e=>{const d=details(e),r=d.rect||{};return {display:d.display,visibility:d.visibility,
      opacity:d.opacity,width:Math.round(r.width||0),height:Math.round(r.height||0),visible:!!d.visible,
      details:d};};
    function collect(root, kind) {
      if (!root || seenRoots.has(root)) return;
      seenRoots.add(root); scopes.push({root,kind});
      let els=[]; try { els=[...root.querySelectorAll('*')]; } catch { return; }
      for(const e of els){
        if(e.shadowRoot&&!seenRoots.has(e.shadowRoot)){trace.openShadowRoots++;collect(e.shadowRoot,'open-shadow');}
        if(e.tagName==='IFRAME'){
          try { const doc=e.contentDocument; if(doc&&!seenRoots.has(doc)){trace.sameOriginFrames++;collect(doc,'same-origin-frame');} }
          catch { trace.inaccessibleFrames++; }
        }
      }
    }
    function snapshot(){
      scopes=[]; seenRoots=new WeakSet(); trace.openShadowRoots=0; trace.sameOriginFrames=0;trace.inaccessibleFrames=0;
      collect(document,'document');
      const dialogs=[], dialogNodes=[], actions=[];
      for(const scope of scopes){
        let els=[];try{els=[...scope.root.querySelectorAll('[role="dialog"],[role="alertdialog"],[aria-modal="true"]')]}catch{}
        for(const e of els){
          dialogNodes.push(e);
          let text='';try{text=e.innerText||e.textContent||''}catch{}
          let controls=[];try{controls=[...e.querySelectorAll('button,[role="button"],[role="menuitem"]')]}catch{}
          const controlStates=controls.map(c=>{let raw='';try{raw=c.innerText||c.getAttribute('aria-label')||''}catch{}
            const l=norm(raw),v=visibility(c);return {label:allowed.has(l)?l:'other',visibility:v,
              visibilityDetails:v.details}});
          const labels=controlStates.map(c=>c.label);
          dialogs.push({scope:scope.kind,tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),
            ariaModal:e.getAttribute('aria-modal')==='true',ariaHidden:e.getAttribute('aria-hidden'),
            hidden:!!e.hidden,visibility:visibility(e),textPresent:!!norm(text),textLength:norm(text).length,
            targetWithdrawalMessage:messageMatch(text),childCount:e.children.length,controlCount:controls.length,
            controlLabels:labels.slice(0,12),controlStates:controlStates.slice(0,12)});
        }
        let buttons=[];try{buttons=[...scope.root.querySelectorAll('button,[role="button"],[role="menuitem"]')]}catch{}
        for(const b of buttons){let raw='';try{raw=b.innerText||b.getAttribute('aria-label')||''}catch{}
          const label=norm(raw);if(!allowed.has(label))continue;
          const v=visibility(b);actions.push({scope:scope.kind,label,
            insideDialog:dialogNodes.some(x=>x===b||x.contains(b)),visibility:v,
            visibilityDetails:v.details});
        }
      }
      const signals=[];
      for(const scope of scopes){let body='';try{body=norm(scope.root.body?.innerText||'')}catch{}
        for(const [kind,phrases] of Object.entries(guardPhrases))
          if(phrases.some(p=>body.includes(norm(p)))&&!signals.includes(kind))signals.push(kind);}
      const entries=performance.getEntriesByType('resource').slice(trace.resourceStart || 0);const net={};
      for(const e of entries){const k=String(e.initiatorType||'other')+':' +
        (typeof e.responseStatus==='number'?String(e.responseStatus):'status-unavailable');net[k]=(net[k]||0)+1;}
      return {dialogs,visibleActions:actions.slice(0,40),openShadowRoots:trace.openShadowRoots,
        sameOriginFrames:trace.sameOriginFrames,inaccessibleFrames:trace.inaccessibleFrames,
        resourceCounts:net,guardSignals:signals,windowErrorCount:trace.errors,unhandledRejectionCount:trace.rejections};
    }
    let last='';
    function sample(reason){const s=snapshot();const sig=JSON.stringify(s);
      if(sig!==last||reason==='failure-snapshot'){last=sig;trace.changes.push({elapsedMs:Math.round(performance.now()-trace.started),reason,state:s});
        if(trace.changes.length>160)trace.changes.shift();}}
    function observe(){for(const s of scopes){if(observedRoots.has(s.root))continue;
      try{observedRoots.add(s.root);
        new MutationObserver(()=>{collectNew();sample('dom-mutation')}).observe(s.root,{subtree:true,childList:true,attributes:true,characterData:true,
          attributeFilter:['role','aria-modal','aria-hidden','hidden','class','style']});}catch{}}
    }
    function collectNew(){for(const s of scopes.slice()){let es=[];try{es=[...s.root.querySelectorAll('*')]}catch{}
      for(const e of es){if(e.shadowRoot&&!seenRoots.has(e.shadowRoot)){trace.openShadowRoots++;collect(e.shadowRoot,'open-shadow')}if(e.tagName==='IFRAME'){try{const doc=e.contentDocument;if(doc&&!seenRoots.has(doc)){trace.sameOriginFrames++;collect(doc,'same-origin-frame')}}catch{trace.inaccessibleFrames++}}}}
      observe();}
    window.addEventListener('error',()=>{trace.errors++;sample('window-error')});
    window.addEventListener('unhandledrejection',()=>{trace.rejections++;sample('unhandled-rejection')});
    window.__igSafeModalTrace=trace;window.__igSafeModalSample=sample;
    trace.resourceStart=performance.getEntriesByType('resource').length;
    collect(document,'document');observe();sample('armed');
    try { trace.resourceObserver=new PerformanceObserver(()=>sample('network-resource'));
      trace.resourceObserver.observe({type:'resource', buffered:false}); } catch {}
    return true;
    """
    try:
        # Installs the exact same read-only visibility inspector used by the
        # action decision. The pre-click result itself is intentionally ignored.
        driver.execute_script(ATOMIC_CONFIRMATION_OBSERVATION_JS, name)
        return bool(driver.execute_script(script, name))
    except Exception:
        return False


def sanitize_modal_timeline(data: dict, name: str) -> dict:
    """Defense-in-depth allowlist for DOM trace data before local persistence."""
    allowed_labels = {"requested", "request sent", "unfollow", "cancel", "iptal",
                      "takibi birak", "istek gonderildi", "takip et", "follow", "other"}
    def safe_visibility(value):
        value = value if isinstance(value, dict) else {}
        display = value.get("display")
        visibility = value.get("visibility")
        try: opacity = round(float(value.get("opacity", 0)), 3)
        except (TypeError, ValueError): opacity = 0.0
        return {"display": display if display in {"none", "block", "flex", "inline", "inline-block", "grid"} else "other",
                "visibility": visibility if visibility in {"visible", "hidden", "collapse"} else "other",
                "opacity": opacity,
                "width": max(0, min(10000, int(value.get("width", 0)))),
                "height": max(0, min(10000, int(value.get("height", 0)))),
                "visible": bool(value.get("visible"))}
    events = []
    for event in data.get("changes", [])[-160:]:
        if not isinstance(event, dict) or not isinstance(event.get("state"), dict):
            continue
        state = event["state"]
        dialogs = []
        for item in state.get("dialogs", [])[:12]:
            if not isinstance(item, dict):
                continue
            control_states = []
            for control in item.get("controlStates", [])[:12]:
                if not isinstance(control, dict):
                    continue
                control_states.append({
                    "label": control.get("label") if control.get("label") in allowed_labels else "other",
                    "visibility": safe_visibility(control.get("visibility")),
                    "visibility_details": sanitize_visibility_details(control.get("visibilityDetails")),
                })
            dialogs.append({
                "scope": item.get("scope") if item.get("scope") in {"document", "open-shadow", "same-origin-frame"} else "other",
                "tag": item.get("tag") if isinstance(item.get("tag"), str) and re.fullmatch(r"[a-z0-9-]{1,24}", item["tag"]) else "other",
                "role": item.get("role") if item.get("role") in {"dialog", "alertdialog", None} else "other",
                "aria_modal": bool(item.get("ariaModal")),
                "aria_hidden": item.get("ariaHidden") if item.get("ariaHidden") in {"true", "false", None} else "other",
                "hidden": bool(item.get("hidden")),
                "visibility": safe_visibility(item.get("visibility")),
                "text_present": bool(item.get("textPresent")),
                "text_length": int(item.get("textLength", 0)),
                "target_withdrawal_message": bool(item.get("targetWithdrawalMessage")),
                "child_count": int(item.get("childCount", 0)),
                "control_count": int(item.get("controlCount", 0)),
                "control_labels": [x if x in allowed_labels else "other" for x in item.get("controlLabels", [])[:12]],
                "control_states": control_states,
            })
        actions = []
        for item in state.get("visibleActions", [])[:40]:
            if not isinstance(item, dict):
                continue
            actions.append({"scope": item.get("scope") if item.get("scope") in {"document", "open-shadow", "same-origin-frame"} else "other",
                            "label": item.get("label") if item.get("label") in allowed_labels else "other",
                            "inside_dialog": bool(item.get("insideDialog")),
                            "visibility": safe_visibility(item.get("visibility")),
                            "visibility_details": sanitize_visibility_details(item.get("visibilityDetails"))})
        resources = {}
        for key, value in state.get("resourceCounts", {}).items():
            if isinstance(key, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,24}:(?:[1-5][0-9]{2}|status-unavailable)", key):
                resources[key] = int(value)
        events.append({"elapsed_ms": max(0, int(event.get("elapsedMs", 0))),
                       "reason": event.get("reason") if event.get("reason") in {"armed", "dom-mutation", "window-error", "unhandled-rejection", "network-resource", "webdriver-sample", "failure-snapshot"} else "other",
                       "state": {"dialogs": dialogs, "visible_actions": actions,
                                 "open_shadow_roots": int(state.get("openShadowRoots", 0)),
                                 "same_origin_frames": int(state.get("sameOriginFrames", 0)),
                                 "inaccessible_frames": int(state.get("inaccessibleFrames", 0)),
                                 "resource_counts": resources,
                                 "guard_signals": [x for x in state.get("guardSignals", []) if x in {"restriction", "security_check"}],
                                 "window_error_count": int(state.get("windowErrorCount", 0)),
                                 "unhandled_rejection_count": int(state.get("unhandledRejectionCount", 0))}})
    return {"target": name, "capture": "sanitized_dom_timeline",
            "started_at_utc": datetime.fromtimestamp(
                data["startedAtEpochMs"] / 1000, timezone.utc).isoformat(timespec="milliseconds"),
            "events": events,
            "window_error_count": int(data.get("windowErrorCount", 0)),
            "unhandled_rejection_count": int(data.get("unhandledRejectionCount", 0))}


def save_modal_timeline(driver, name: str) -> str:
    """Persist the sanitized event buffer only after an ambiguous modal outcome."""
    try:
        data = driver.execute_script("""
          if(!window.__igSafeModalTrace)return null;
          window.__igSafeModalSample('failure-snapshot');
          return {startedAtEpochMs:Date.now()-(performance.now()-window.__igSafeModalTrace.started),
            changes:window.__igSafeModalTrace.changes,
            windowErrorCount:window.__igSafeModalTrace.errors,
            unhandledRejectionCount:window.__igSafeModalTrace.rejections};
        """)
        if not isinstance(data, dict):
            return ""
        payload = sanitize_modal_timeline(data, name)
        atomic_write_json(MODAL_TRACE_PATH, payload)
        return f" Zaman çizelgesi: {MODAL_TRACE_PATH}"
    except Exception:
        return ""


def profile_state(driver) -> str:
    """Yalnızca profil başlığından durumu oku; başka hesaplara dokunma."""
    if find_profile_button(driver, is_requested):
        return "requested"
    if find_profile_button(driver, is_follow):
        return "follow"
    if find_profile_button(driver, is_following):
        return "following"
    return "unknown"


def is_unavailable_profile_page(driver, name: str) -> bool:
    """Only treat an explicit Instagram error on the target URL as unavailable.

    Never infer account deletion from a missing button or generic page failure.
    Other unknown pages, logins, rate limits, and UI bugs still stop the run.
    """
    from selenium.webdriver.common.by import By
    location = urlsplit(driver.current_url)
    if (location.hostname or "").lower() not in {
        "instagram.com", "www.instagram.com", "m.instagram.com"
    } or location.path.rstrip("/").casefold() != f"/{name.casefold()}":
        return False
    body = driver.find_elements(By.TAG_NAME, "body")
    if not body:
        return False
    visible = normalize(body[0].text[:2500]).replace("ı", "i")
    return (
        "sorry, this page isn't available" in visible
        and "the link you followed may be broken" in visible
    ) or (
        "uzgunuz" in visible
        and ("bu sayfa kullanilamiyor" in visible or "bu sayfaya ulasilamiyor" in visible)
    )


def wait_profile_state(driver, timeout: float = 20.0, screenshot_on_fail: bool = True, expected_user: str | None = None) -> str:
    from selenium.webdriver.support.ui import WebDriverWait
    def recognized(d):
        # Bir düğme bulunamadığı için değil, yalnızca açık hata metni varsa ertele.
        if expected_user and is_unavailable_profile_page(d, expected_user):
            raise ProfileUnavailableError(
                "Instagram bu profil için 'Sorry, this page isn't available' gösterdi. "
                "İstek iptali denenmedi; hesap erişilemiyor veya kullanıcı adı değişmiş olabilir."
            )
        state = profile_state(d)
        return state if state != "unknown" else False
    try:
        return WebDriverWait(driver, timeout, poll_frequency=0.5).until(recognized)
    except ProfileUnavailableError:
        raise
    except Exception as exc:
        guard_session(driver)
        debug = save_debug_screenshot(driver) if screenshot_on_fail else ""
        raise RuntimeError(f"Profil düğmesi tanınamadı; işlem yapılmadı.{debug}") from exc


def save_ui_state(driver) -> str:
    """Kişisel sayfa metnini veya şifreyi toplamadan eylem düğmelerini kaydet."""
    from selenium.webdriver.common.by import By
    snapshot = {"profile_state": profile_state(driver), "controls": []}
    xpath = (
        "//main//header//button | //main//header//*[@role='button'] | "
        "//*[@role='dialog']//*[self::button or @role='button'] | "
        "//*[@role='menu']//*[self::button or @role='button' or @role='menuitem']"
    )
    try:
        for element in driver.find_elements(By.XPATH, xpath):
            if element.is_displayed():
                label = element_label(element)
                if label and label not in snapshot["controls"]:
                    snapshot["controls"].append(label[:100])
        atomic_write_json(UI_STATE_PATH, snapshot)
        return f" UI tanısı: {UI_STATE_PATH}"
    except Exception:
        return ""


def guard_session(driver) -> None:
    url = driver.current_url.lower()
    if any(x in url for x in ("/challenge/", "/accounts/login/", "/suspended/")):
        raise RuntimeError("Instagram giriş, güvenlik kontrolü veya doğrulama sayfasına geçti; işlem durduruldu.")
    from selenium.webdriver.common.by import By
    body = driver.find_elements(By.TAG_NAME, "body")
    visible = normalize(body[0].text[:3500]) if body else ""
    for phrase in (
        "try again later", "please wait a few minutes", "we restrict certain activity",
        "lutfen daha sonra tekrar dene", "bir kac dakika bekle", "islem engellendi",
        "captcha", "confirm it is you", "security check", "unusual activity",
        "automated behavior", "automated activity", "otomatik etkinlik",
        "guvenlik kontrolu", "dogrulama gerekli",
    ):
        if normalize(phrase) in visible:
            raise RuntimeError("Instagram güvenlik doğrulaması, otomasyon uyarısı veya işlem kısıtlaması gösterdi; işlem durduruldu.")


def logged_in_username(driver) -> str | None:
    """Identify the signed-in owner from their own visible profile page.

    Current Instagram desktop navigation no longer labels the own-profile link
    with aria-label="Profile". Require both the exact profile URL/heading and
    an owner-only edit-profile control before treating the account as verified.
    """
    location = urlsplit(driver.current_url)
    path_user = username_from_instagram_url(driver.current_url)
    if ((location.hostname or "").lower() not in {
            "instagram.com", "www.instagram.com", "m.instagram.com"}
            or path_user is None or location.path.strip("/").count("/") != 0):
        return None
    try:
        from selenium.webdriver.common.by import By
        headers = [e for e in driver.find_elements(By.XPATH, "//main//header") if e.is_displayed()]
        if len(headers) != 1:
            return None
        lines = [normalize(line).replace("ı", "i") for line in headers[0].text.splitlines() if line.strip()]
        has_owner_control = any(
            marker in " ".join(lines)
            for marker in ("edit profile", "profili duzenle")
        )
        if path_user.casefold() in {line.replace("ı", "i") for line in lines} and has_owner_control:
            return path_user.casefold()
    except Exception:
        return None
    return None


def verify_account_identity(driver, expected: str) -> bool:
    """Navigate only to the expected user's own page and verify its owner UI."""
    from selenium.webdriver.support.ui import WebDriverWait
    driver.get(f"https://www.instagram.com/{expected}/")
    guard_session(driver)
    try:
        return WebDriverWait(driver, 20, poll_frequency=0.5).until(
            lambda d: logged_in_username(d) == expected.casefold()
        ) is True
    except Exception:
        guard_session(driver)
        return False


def save_debug_screenshot(driver) -> str:
    """Tıklamayı engelleyen UI'ı analiz etmek için yerel ekran görüntüsü."""
    location = SCREENSHOT_PATH
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        if driver.save_screenshot(str(location)):
            return f" Ekran görüntüsü: {location}"
    except Exception:
        pass
    return ""


def click_when_unobscured(driver, find_target, label: str, timeout: float = 15.0) -> None:
    """Yalnızca gerçek kullanıcı tıklamasının hedefe ulaşacağı anda tıkla.

    JavaScript burada yalnızca konumu/engeli okur ve kaydırma yapar;
    düğmeyi JavaScript ile tıklamıyoruz. Engellenmiş düğmeye basmayı zorlamıyoruz.
    """
    from selenium.common.exceptions import (
        ElementClickInterceptedException,
        StaleElementReferenceException,
    )
    from selenium.webdriver.support.ui import WebDriverWait

    def find_clear_element(d):
        try:
            element = find_target(d)
            if not element:
                return False
            d.execute_script(
                "arguments[0].scrollIntoView({block:'center',inline:'nearest',behavior:'auto'});",
                element,
            )
            # Selenium click, WebDriver standardına göre elementin orta noktasını hedefler.
            unobscured = d.execute_script(
                """
                const el = arguments[0];
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                if (!el.isConnected || el.hidden || el.disabled ||
                    el.getAttribute('aria-hidden') === 'true' ||
                    el.getAttribute('aria-disabled') === 'true' ||
                    r.width <= 0 || r.height <= 0 || s.display === 'none' ||
                    s.visibility === 'hidden' || s.visibility === 'collapse' ||
                    Number(s.opacity) <= 0) return false;
                const x = Math.max(0, Math.min(window.innerWidth - 1, r.left + r.width / 2));
                const y = Math.max(0, Math.min(window.innerHeight - 1, r.top + r.height / 2));
                if (x < r.left || x >= r.right || y < r.top || y >= r.bottom) return false;
                const top = document.elementFromPoint(x, y);
                return !!top && (top === el || el.contains(top));
                """,
                element,
            )
            return element if unobscured else False
        except (StaleElementReferenceException,):
            return False

    for attempt in range(3):
        try:
            element = WebDriverWait(
                driver,
                timeout,
                poll_frequency=0.5,
                ignored_exceptions=(StaleElementReferenceException,),
            ).until(find_clear_element)
        except Exception as exc:
            guard_session(driver)
            debug = save_debug_screenshot(driver)
            raise RuntimeError(
                f"{label}: Düğme 15 saniye boyunca başka bir öğeyle örtülü kaldı "
                "veya kayboldu. Firefox'taki çerez/onay pencerelerini ve "
                f"açılır panelleri kontrol et.{debug}"
            ) from exc
        try:
            element.click()
            return
        except (ElementClickInterceptedException, StaleElementReferenceException):
            guard_session(driver)
            if attempt == 2:
                debug = save_debug_screenshot(driver)
                raise RuntimeError(
                    f"{label}: Firefox tıklama anında düğmenin önüne başka bir öğe geçti."
                    f"{debug}"
                ) from None
            # Animasyon / geçici bildirim yüzünden olabilir; hedefi baştan bul.
            time.sleep(0.8)


def open_profile_fresh(driver, name: str) -> None:
    """Önceki React/modal belgesini bırak; aynı Firefox oturumunu koru.

    Yalnızca sayfa navigasyonu yapar; herhangi bir Instagram eylemini tekrarlamaz.
    Selenium profil çerezleri about:blank geçişinde silinmez.
    """
    driver.get("about:blank")
    driver.get(f"https://www.instagram.com/{name}/")
    guard_session(driver)


def visible_empty_dialog(driver, name: str = "") -> bool:
    """Use the same atomic DOM observation as confirmation selection.

    A dialog is empty only when every currently visible dialog candidate has
    neither normalized text nor a visible control. A second stale/placeholder
    node therefore cannot override a populated target dialog.
    """
    return observe_confirmation_dom(driver, name).get("status") == "empty"


def empty_dialog_persists(driver, name: str = "", grace_seconds: float = 1.2,
                          poll_seconds: float = 0.1) -> bool:
    """Return true only if a visible empty dialog remains empty across a short grace period.

    Instagram may commit the backdrop/dialog shell before React inserts the
    confirmation content. This observes the DOM only; it never clicks a control.
    A dialog still empty after the grace period remains a hard stop.
    """
    started = time.monotonic()
    while time.monotonic() - started < grace_seconds:
        if not visible_empty_dialog(driver, name):
            return False
        time.sleep(poll_seconds)
    return visible_empty_dialog(driver, name)


def cancel_one(driver, name: str) -> str:
    """Bir kez tıkla, sonucu doğrula; belirsiz eylemi ASLA yeniden tıklama."""
    open_profile_fresh(driver, name)
    initial = wait_profile_state(driver, expected_user=name)
    if initial != "requested":
        # Takip Et / Takiptesin: bekleyen istek yok; herhangi bir düğmeye dokunma.
        return "not_pending"

    # document.readyState=complete, React olay dinleyicilerinin de hazır olduğunu
    # garanti etmez. Kısa bir stabilizasyon ve salt okunur tekrar kontrolü.
    time.sleep(1.8)
    guard_session(driver)
    settled = wait_profile_state(driver, timeout=12, expected_user=name)
    if settled != "requested":
        return "not_pending"

    # Keep an in-memory, redacted DOM timeline so a later blank-modal failure
    # can be distinguished from a short React render gap or inaccessible portal.
    arm_modal_timeline(driver, name)
    click_when_unobscured(
        driver,
        lambda d: find_profile_button(d, is_requested),
        "İstek Gönderildi",
    )

    # Yalnız doğrulanmış opacity render geçişini en fazla 15 saniye bekle.
    # Ambiguous/empty/identity/security durumları wait içinde hemen durur.
    try:
        wait_for_valid_confirmation(driver, name, timeout=15.0, poll_seconds=0.25)
    except (EmptyConfirmationDialogError, AmbiguousConfirmationDialogError) as exc:
        timeout_observation = (
            exc.timeout_observation
            if isinstance(exc, RenderingTimeoutBeforeConfirmationClick) else None
        )
        diagnostics = (save_modal_timeline(driver, name)
                       + save_ui_state(driver)
                       + save_atomic_modal_evidence(driver, name, timeout_observation))
        if isinstance(exc, RenderingTimeoutBeforeConfirmationClick):
            raise RenderingTimeoutBeforeConfirmationClick(
                f"{exc}{diagnostics}", timeout_observation=timeout_observation
            ) from exc
        raise type(exc)(f"{exc}{diagnostics}") from exc
    click_when_unobscured(
        driver, lambda d: find_confirmation(d, name), "İsteği iptal et", timeout=8,
    )
    confirmed_once = True

    # Onay yalnızca bir kez tıklandı; sonuç belirsizse asla yeniden tıklama.
    deadline = time.monotonic() + 34
    while time.monotonic() < deadline:
        guard_session(driver)
        if profile_state(driver) == "follow":
            return "cancelled"
        time.sleep(0.5)

    # Onay seçilemedi veya tıklandıktan sonra sonuç gözlemlenemedi. ÖNCE
    # ekrandaki popup'ı kaydet; SONRA yalnızca görüntüleyerek yeniden kontrol et.
    empty_dialog = visible_empty_dialog(driver, name)
    diagnostics = (save_modal_timeline(driver, name)
                   + save_ui_state(driver) + save_atomic_modal_evidence(driver, name))
    guard_session(driver)
    # Görünmeyen modalin ardından yalnızca sayfayı sıfırla ve durumu OKU;
    # ikinci kez Requested/Unfollow tıklamak YASAK.
    open_profile_fresh(driver, name)
    try:
        final_state = wait_profile_state(driver, timeout=16, screenshot_on_fail=False, expected_user=name)
    except RuntimeError:
        final_state = "unknown"

    if final_state == "follow":
        # Yenileme sonrası açıkça Takip Et görülüyorsa iptal doğrulandı.
        return "cancelled"
    # İstek hâlâ bekliyor olabilir veya doğrulanamıyor. Tekrar tıklamak yok.
    reason = {
        "requested": "istek hâlâ gönderilmiş görünüyor",
        "following": "hesap artık takip ediliyor görünüyor",
        "unknown": "profil durumu okunamıyor",
    }.get(final_state, "durum belirsiz")
    detail = " Boş Instagram onay penceresi algılandı." if empty_dialog else ""
    if empty_dialog:
        exception_type = EmptyConfirmationDialogError
    elif confirmed_once:
        exception_type = AmbiguousCancellationError
    else:
        exception_type = AmbiguousConfirmationDialogError
    action_detail = ("İptal tıklaması doğrulanamadı" if confirmed_once else
                     "Güvenli onay kontrolü seçilemedi; onay düğmesine basılmadı")
    raise exception_type(
        f"{action_detail} ({reason}).{detail} Aynı hesaba ikinci kez "
        f"otomatik tıklanmadı.{diagnostics}"
    )


def ambiguity_stop_reason(attempts: list[bool]) -> str | None:
    """Return the fail-closed reason for ambiguity thresholds, if reached."""
    if len(attempts) >= 2 and attempts[-2:] == [True, True]:
        return "art arda iki belirsizlik"
    if sum(attempts[-20:]) >= 3:
        return "son 20 denemede üç belirsizlik"
    return None


def _proc_identity(pid: int) -> tuple[int, int, str] | None:
    """Read (ppid, starttime, command) without signalling any process."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        end = raw.rfind(")")
        fields = raw[end + 2:].split()
        command = Path(f"/proc/{int(pid)}/comm").read_text(encoding="utf-8").strip()
        return int(fields[1]), int(fields[19]), command
    except (OSError, ValueError, IndexError):
        return None


def capture_owned_driver_processes(driver) -> set[tuple[int, int]]:
    """Snapshot only this WebDriver's geckodriver/Firefox process tree."""
    roots = set()
    service_process = getattr(getattr(driver, "service", None), "process", None)
    service_pid = getattr(service_process, "pid", None)
    browser_pid = (getattr(driver, "capabilities", {}) or {}).get("moz:processID")
    for value in (service_pid, browser_pid):
        if isinstance(value, int) and value > 1:
            roots.add(value)
    if not roots:
        raise RuntimeError("WebDriver/Firefox süreç kimlikleri alınamadı; oturum güvenle yenilenemez.")

    records = {}
    try:
        candidates = [p for p in Path("/proc").iterdir() if p.name.isdigit()]
    except OSError as exc:
        raise RuntimeError("Süreç tablosu okunamadı; Firefox oturumu güvenle yenilenemez.") from exc
    for path in candidates:
        record = _proc_identity(int(path.name))
        if record:
            records[int(path.name)] = record
    owned = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _start, _command) in records.items():
            if ppid in owned and pid not in owned:
                owned.add(pid)
                changed = True
    identities = {(pid, records[pid][1]) for pid in owned if pid in records}
    if not roots.issubset({pid for pid, _start in identities}):
        raise RuntimeError("WebDriver/Firefox ana süreçleri doğrulanamadı; oturum güvenle yenilenemez.")
    return identities


def owned_process_is_alive(identity: tuple[int, int]) -> bool:
    pid, starttime = identity
    current = _proc_identity(pid)
    return bool(current and current[1] == starttime)


def close_driver_and_verify(driver, timeout: float = 12.0) -> None:
    """Quit one owned session and prove its captured processes all exited."""
    identities = capture_owned_driver_processes(driver)
    try:
        driver.quit()
    except Exception as exc:
        raise RuntimeError("WebDriver temiz biçimde kapatılamadı; otomasyon durduruldu.") from exc
    deadline = time.monotonic() + timeout
    while any(owned_process_is_alive(item) for item in identities):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Önceki WebDriver/Firefox süreçleri sona ermedi; başka Firefox "
                "oturumlarına dokunulmadan otomasyon durduruldu."
            )
        time.sleep(0.1)


def selenium_firefox_service(service_class, gecko: str):
    """Give only the WebDriver-owned Firefox a stable Wayland app_id/class."""
    service_env = os.environ.copy()
    service_env["MOZ_APP_REMOTINGNAME"] = SELENIUM_FIREFOX_APP_ID
    return service_class(executable_path=gecko, env=service_env)


def start_verified_driver(webdriver, options, service_class, gecko: str,
                          expected_account: str):
    """Create a fresh profile-backed session and reverify the signed-in owner."""
    driver = webdriver.Firefox(
        options=options,
        service=selenium_firefox_service(service_class, gecko),
    )
    try:
        driver.set_page_load_timeout(45)
        driver.set_window_size(1360, 850)
        driver.get("https://www.instagram.com/")
        guard_session(driver)
        if not verify_account_identity(driver, expected_account):
            raise RuntimeError(
                f"The active Instagram account could not be verified as @{expected_account}."
            )
        return driver
    except Exception:
        close_driver_and_verify(driver)
        raise


def recovery_evidence_is_persisted(name: str) -> bool:
    """Re-read the four durable diagnostics for this exact render timeout."""
    try:
        timeline = json.loads(MODAL_TRACE_PATH.read_text(encoding="utf-8"))
        diagnostic = json.loads(MODAL_DIAGNOSTIC_PATH.read_text(encoding="utf-8"))
        ui_state = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
        png = SCREENSHOT_PATH.read_bytes()
        events = timeline.get("events") if isinstance(timeline, dict) else None
        return (
            timeline.get("target", "").casefold() == name.casefold()
            and isinstance(events, list) and bool(events)
            and int(events[-1].get("elapsed_ms", 0)) >= 14500
            and diagnostic.get("requested_target", "").casefold() == name.casefold()
            and diagnostic.get("observation_status") == "rendering"
            and diagnostic.get("profile_state") == "requested"
            and diagnostic.get("url_matches_target") is True
            and int(diagnostic.get("rendering_dialog_count", 0)) == 1
            and isinstance(ui_state, dict) and ui_state.get("profile_state") == "requested"
            and png.startswith(b"\x89PNG\r\n\x1a\n")
            and SCREENSHOT_PATH.stat().st_mtime_ns >= MODAL_TRACE_PATH.stat().st_mtime_ns
            and MODAL_DIAGNOSTIC_PATH.stat().st_mtime_ns >= SCREENSHOT_PATH.stat().st_mtime_ns
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def deferred_apply_candidates(users: list[str], deferred: dict[str, dict],
                              reviews: dict[str, dict]) -> list[str]:
    """Return only reviewed active requests, preserving archive order."""
    return [
        name for name in users
        if name.casefold() in deferred
        and reviews.get(name.casefold(), {}).get("classification") == "active_requested"
        and not reviews.get(name.casefold(), {}).get("resolution")
        and reviews.get(name.casefold(), {}).get("manual_review_required") is not True
    ]


def load_deferred_allowlist(path: Path, source_hash: str,
                            candidates: list[str]) -> list[str]:
    """Load a fail-closed allowlist that can only narrow safe candidates."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Deferred allowlist okunamıyor; hiçbir işlem yapılmayacak.") from exc
    accounts = document.get("accounts") if isinstance(document, dict) else None
    if (not isinstance(document, dict)
            or document.get("source_sha256") != source_hash
            or not isinstance(accounts, list)):
        raise RuntimeError("Deferred allowlist arşivle uyuşmuyor veya biçimi bozuk.")
    normalized = []
    for name in accounts:
        if not isinstance(name, str) or not valid_username(name):
            raise RuntimeError("Deferred allowlist geçersiz kullanıcı adı içeriyor.")
        key = name.casefold()
        if key in normalized:
            raise RuntimeError("Deferred allowlist yinelenen kullanıcı adı içeriyor.")
        normalized.append(key)
    safe = {name.casefold(): name for name in candidates}
    if any(name not in safe for name in normalized):
        raise RuntimeError(
            "Deferred allowlist güvenli, çözülmemiş active_requested adayları dışında kayıt içeriyor."
        )
    return [safe[name] for name in normalized]


def run_deferred_mode(args, users: list[str], digest: str, processed: dict[str, str],
                      deferred: dict[str, dict], reviews: dict[str, dict]) -> int:
    """Run the separate deferred-only review/apply workflow."""
    if args.deferred_report:
        summary = write_deferred_reports(digest, deferred, reviews)
        print(f"Deferred raporu: incelenen={summary['reviewed']} "
              f"incelenmemiş={summary['unreviewed']} manuel={summary['manual_review']}")
        print(f"JSON: {DEFERRED_REVIEW_PATH}")
        print(f"CSV: {DEFERRED_REVIEW_CSV_PATH}")
        print(f"Markdown: {DEFERRED_REVIEW_MD_PATH}")
        return EXIT_OK

    ordered_deferred = [name for name in users if name.casefold() in deferred]
    if args.deferred_review:
        queue = [name for name in ordered_deferred if name.casefold() not in reviews]
    else:
        queue = deferred_apply_candidates(ordered_deferred, deferred, reviews)
        if args.deferred_allowlist:
            queue = load_deferred_allowlist(
                args.deferred_allowlist.expanduser(), digest, queue)
    if args.limit:
        queue = queue[:args.limit]
    if not queue:
        summary = write_deferred_reports(digest, deferred, reviews)
        print("Bu deferred modu için işlenecek hesap yok.")
        print(f"İncelenen={summary['reviewed']} incelenmemiş={summary['unreviewed']}")
        return EXIT_OK

    if args.apply_deferred and not args.non_interactive:
        answer = input(
            "Type APPLY to process only previously verified active_requested deferred accounts: "
        ).strip()
        if answer != "APPLY":
            print("Cancelled without making changes.")
            return EXIT_OK

    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    firefox = shutil.which("firefox")
    gecko = shutil.which("geckodriver")
    if not firefox or not gecko:
        print("HATA: Firefox/Geckodriver bulunamadı. Betiği Nix ortamında çalıştır.",
              file=sys.stderr)
        return EXIT_DATA_ERROR
    PROFILE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    options = Options()
    options.binary_location = firefox
    options.add_argument("-profile")
    options.add_argument(str(PROFILE_DIR.resolve()))
    lock_handle = None
    driver = None
    result_code = EXIT_OK
    reviewed_now = cancelled_now = resolved_now = 0
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_handle = LOCK_PATH.open("a", encoding="utf-8")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Başka bir temizleme/inceleme süreci çalışıyor; deferred modu başlatılmadı."
            ) from exc
        driver = start_verified_driver(webdriver, options, Service, gecko, args.account)
        print(f"Deferred mode: active account verified as @{args.account}.")
        for index, name in enumerate(queue, 1):
            key = name.casefold()
            old_reason = deferred[key].get("reason", "unknown")
            print(f"[{index}/{len(queue)}] @{name}: ", end="", flush=True)
            try:
                classification = classify_deferred_profile(driver, name, old_reason)
                reviews[key] = classification
                write_deferred_reports(digest, deferred, reviews)
                reviewed_now += 1
                if args.deferred_review:
                    print(f"{classification['classification']} "
                          f"({classification['evidence_type']})")
                elif classification["classification"] == "active_requested":
                    # Re-open and re-validate everything in cancel_one. It is the only
                    # path that can click a confirmation control.
                    action = cancel_one(driver, name)
                    if action not in {"cancelled", "not_pending"}:
                        raise RuntimeError("Deferred iptal sonucu tanınmıyor; işlem durduruldu.")
                    processed[key] = action
                    save_progress(digest, processed)
                    audit_event(
                        name, action,
                        "profile_state=follow" if action == "cancelled"
                        else "profile_state=follow_before_action",
                    )
                    del deferred[key]
                    save_deferred(digest, deferred)
                    reviews[key]["resolution"] = action
                    reviews[key]["resolved_at_utc"] = datetime.now(timezone.utc).isoformat(
                        timespec="seconds")
                    write_deferred_reports(digest, deferred, reviews)
                    if action == "cancelled":
                        cancelled_now += 1
                        print("active_requested → iptal doğrulandı ✓")
                    else:
                        resolved_now += 1
                        print("Yeniden doğrulamada bekleyen istek yok; tıklanmadı")
                elif classification["classification"] in {
                        "active_not_pending", "active_following"}:
                    processed[key] = "not_pending"
                    save_progress(digest, processed)
                    audit_event(
                        name, "not_pending",
                        f"deferred_review={classification['classification']}; confirmation_not_clicked",
                    )
                    del deferred[key]
                    save_deferred(digest, deferred)
                    reviews[key]["resolution"] = "not_pending"
                    reviews[key]["resolved_at_utc"] = datetime.now(timezone.utc).isoformat(
                        timespec="seconds")
                    write_deferred_reports(digest, deferred, reviews)
                    resolved_now += 1
                    print(f"{classification['classification']}; Unfollow tıklanmadı")
                else:
                    print(f"DURDU: yeniden doğrulama {classification['classification']}; "
                          "hiçbir onay tıklanmadı")
                    result_code = EXIT_TRANSIENT
                    break
            except ProfileUnavailableError:
                reviews[key] = deferred_review_record(
                    old_reason, "unavailable_unknown",
                    "explicit_page_unavailable_message", "high")
                write_deferred_reports(digest, deferred, reviews)
                print("unavailable_unknown; hiçbir kontrol tıklanmadı")
                if args.apply_deferred:
                    result_code = EXIT_TRANSIENT
                    break
            except (AmbiguousCancellationError, AmbiguousConfirmationDialogError,
                    EmptyConfirmationDialogError) as exc:
                print(f"GÜVENLİK DURUŞU: {exc}")
                result_code = EXIT_SAFETY_STOP
                break
            except RuntimeError as exc:
                print(f"GÜVENLİK DURUŞU: {exc}")
                result_code = EXIT_SAFETY_STOP
                break
            except Exception as exc:
                log_unexpected_operational_error(name, exc)
                result_code = EXIT_TRANSIENT
                break
            if index < len(queue):
                time.sleep(random.uniform(args.min_delay, args.max_delay))
    except RuntimeError as exc:
        print(f"GÜVENLİK DURUŞU: {exc}", file=sys.stderr)
        result_code = EXIT_SAFETY_STOP
    except Exception as exc:
        print(f"GEÇİCİ ÇALIŞMA HATASI: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        result_code = EXIT_TRANSIENT
    finally:
        if driver is not None:
            try:
                close_driver_and_verify(driver)
            except RuntimeError as exc:
                print(f"GÜVENLİK DURUŞU: {exc}", file=sys.stderr)
                result_code = EXIT_SAFETY_STOP
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()
    summary = write_deferred_reports(digest, deferred, reviews)
    print(f"Deferred turu: incelenen={reviewed_now}, iptal={cancelled_now}, "
          f"bekleyen_yok={resolved_now}, kalan={summary['deferred_total']}, "
          f"incelenmemiş={summary['unreviewed']}")
    return result_code


def persist_render_timeout_defer(source_hash: str, usernames: set[str], deferred: dict,
                                 name: str) -> None:
    """Durably defer and audit one pre-click render timeout, then re-read it."""
    defer_user(deferred, name, "rendering_timeout_no_confirmation_click")
    save_deferred(source_hash, deferred)
    audit_event(name, "deferred", "rendering_timeout; confirmation_not_clicked")
    validate_state_integrity(source_hash, usernames)
    stored = load_deferred(source_hash).get(name.casefold(), {})
    lines = [line for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines() if line]
    last = json.loads(lines[-1]) if lines else {}
    if (stored.get("reason") != "rendering_timeout_no_confirmation_click"
            or last.get("username", "").casefold() != name.casefold()
            or last.get("result") != "deferred"
            or last.get("verification") != "rendering_timeout; confirmation_not_clicked"
            or not recovery_evidence_is_persisted(name)):
        raise RuntimeError(
            "Render zaman aşımı tanıları, erteleme veya audit kaydı kalıcı "
            "doğrulanamadı; sıradaki hesaba geçilmedi."
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="instagram-request-cleaner",
        description="Safely review and withdraw pending sent Instagram follow requests.")
    parser.add_argument("--archive", required=True, type=Path,
                        help="Instagram export ZIP, JSON file, or extracted export directory")
    parser.add_argument("--account", help="Expected signed-in Instagram username")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir(),
                        help="Writable state directory (default: XDG_STATE_HOME/instagram-request-cleaner)")
    parser.add_argument("--profile-dir", type=Path,
                        help="Dedicated Firefox profile directory (default: STATE_DIR/firefox-profile)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Read the export and preview pending accounts; never open Firefox")
    parser.add_argument("--apply", action="store_true", help="Withdraw requests instead of previewing")
    parser.add_argument("--status", action="store_true", help="Show local counts without opening Firefox")
    parser.add_argument("--diagnose", action="store_true", help="Show a redacted schema summary for unresolved export entries")
    parser.add_argument("--inspect-user", metavar="USERNAME", help="Read one exported account state without clicking")
    parser.add_argument("--reconcile-user", metavar="USERNAME", help="Read one deferred account and record not-pending only when verified")
    parser.add_argument("--defer-user", metavar="USERNAME", help="Defer one exported account without opening Instagram")
    parser.add_argument("--defer-unverified", action="store_true", help="Persist uncertainty and stop with a safety exit")
    parser.add_argument("--retry-deferred", action="store_true", help="Operate only on deferred records")
    parser.add_argument("--deferred-review", action="store_true", help="Classify unreviewed deferred accounts read-only")
    parser.add_argument("--deferred-report", action="store_true", help="Generate deferred JSON/CSV/Markdown reports")
    parser.add_argument("--apply-deferred", action="store_true", help="Apply only previously verified active_requested deferred records")
    parser.add_argument("--deferred-allowlist", type=Path, help="Narrow --apply-deferred candidates with a digest-bound JSON allowlist")
    parser.add_argument("--limit", type=int, default=0, help="Maximum accounts for this run (0 = all candidates)")
    parser.add_argument("--min-delay", type=float, default=8.0, help="Minimum delay between accounts in seconds")
    parser.add_argument("--max-delay", type=float, default=15.0, help="Maximum delay between accounts in seconds")
    parser.add_argument("--non-interactive", action="store_true", help="Never prompt; stop if pre-authorized safety conditions are not met")
    parser.add_argument("--deadline", help="Timezone-aware ISO deadline (legacy: next-istanbul-0800)")
    args = parser.parse_args()
    configure_runtime_paths(args.state_dir, args.profile_dir)
    deferred_modes = sum(bool(value) for value in (
        args.deferred_review, args.deferred_report, args.apply_deferred))
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run cannot be used together")
    browser_mode = bool(args.apply or args.inspect_user or args.reconcile_user
                        or args.deferred_review or args.apply_deferred)
    if browser_mode and (not args.account or not valid_username(args.account)):
        parser.error("browser modes require --account with a valid Instagram username")
    if deferred_modes > 1:
        parser.error("--deferred-review/--deferred-report/--apply-deferred birlikte kullanılamaz")
    if args.deferred_allowlist and not args.apply_deferred:
        parser.error("--deferred-allowlist yalnız --apply-deferred ile kullanılabilir")
    if args.status and (args.apply or args.inspect_user or args.reconcile_user or args.defer_user
                        or args.retry_deferred or args.defer_unverified or deferred_modes):
        parser.error("--status tek başına kullanılmalı; yalnızca kayıtları gösterir")
    if deferred_modes and (args.apply or args.inspect_user or args.reconcile_user
                           or args.defer_user or args.retry_deferred or args.defer_unverified):
        parser.error("Deferred inceleme modları mevcut işlem seçeneklerinden ayrı kullanılmalı")
    if args.reconcile_user and (args.apply or args.inspect_user or args.defer_user or args.retry_deferred or args.defer_unverified):
        parser.error("--reconcile-user tek başına kullanılmalı")
    if args.reconcile_user and not valid_username(args.reconcile_user):
        parser.error("Geçerli bir Instagram kullanıcı adı yaz")
    if args.inspect_user and args.apply:
        parser.error("--inspect-user ile --apply birlikte kullanılamaz")
    if args.inspect_user and (args.defer_user or args.retry_deferred or args.defer_unverified):
        parser.error("--inspect-user diğer ertelenmiş işlem seçenekleriyle birlikte kullanılamaz")
    if args.defer_user and (args.apply or args.defer_unverified or args.retry_deferred):
        parser.error("--defer-user tek başına kullanılmalı; hiçbir tıklama yapmaz")
    if (args.defer_unverified or args.retry_deferred) and not args.apply:
        parser.error("--defer-unverified/--retry-deferred için --apply gerekli")
    if args.inspect_user and not valid_username(args.inspect_user):
        parser.error("Geçerli bir Instagram kullanıcı adı yaz")
    if args.defer_user and not valid_username(args.defer_user):
        parser.error("Ertelemek için geçerli bir Instagram kullanıcı adı yaz")
    if args.limit < 0 or args.min_delay < 5 or args.max_delay < args.min_delay:
        parser.error("--limit >= 0, --min-delay >= 5, --max-delay >= --min-delay olmalı")
    deadline = None
    if args.deadline:
        try:
            if args.deadline == "next-istanbul-0800":
                local_now = datetime.now(ZoneInfo("Europe/Istanbul"))
                deadline = local_now.replace(hour=8, minute=0, second=0, microsecond=0)
                if deadline <= local_now:
                    from datetime import timedelta
                    deadline += timedelta(days=1)
                print(f"Gece çalışma son tarihi: {deadline.isoformat()}")
            else:
                deadline = datetime.fromisoformat(args.deadline)
            if deadline.tzinfo is None:
                raise ValueError("timezone gerekli")
            deadline = deadline.astimezone(timezone.utc)
            if deadline <= datetime.now(timezone.utc):
                parser.error("--deadline gelecekte olmalı; hiçbir Instagram işlemi başlatılmadı")
        except ValueError:
            parser.error("--deadline timezone içeren ISO biçiminde olmalı")
    if args.non_interactive and not (
            args.deferred_review or args.apply_deferred
            or (args.apply and args.defer_unverified and not args.retry_deferred)):
        parser.error("--non-interactive için --apply ve --defer-unverified gerekli; ertelenenler yeniden denenemez")

    try:
        users, digest = read_export(args.archive.expanduser(), diagnose=args.diagnose)
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"HATA: {exc}", file=sys.stderr)
        return 2

    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    usernames = {u.casefold() for u in users}
    if args.apply or deferred_modes:
        try:
            validate_state_integrity(digest, usernames)
        except RuntimeError as exc:
            print(f"DURDU: {exc}", file=sys.stderr)
            return 2
    processed = load_progress(digest)
    deferred = load_deferred(digest)
    reviews = {}
    if args.status or deferred_modes:
        try:
            reviews = load_deferred_review(digest, usernames)
        except RuntimeError as exc:
            print(f"DURDU: {exc}", file=sys.stderr)
            return EXIT_DATA_ERROR
    if args.reconcile_user and args.reconcile_user.casefold() not in usernames:
        print("HATA: Kullanıcı arşivde bulunamadı.", file=sys.stderr)
        return 2
    if args.reconcile_user and args.reconcile_user.casefold() not in deferred:
        print("HATA: Kullanıcı ertelenenler listesinde değil.", file=sys.stderr)
        return 2
    if args.inspect_user and args.inspect_user.casefold() not in usernames:
        print("HATA: Bu kullanıcı adı dışa aktarım dosyasında bulunamadı.", file=sys.stderr)
        return 2
    if args.defer_user:
        name = args.defer_user.casefold()
        if name not in usernames:
            print("HATA: Bu kullanıcı adı arşivde yok.", file=sys.stderr)
            return 2
        if name in processed:
            print(f"@{name}: Önceden işlenmiş. İlerleme kaydına dokunulmadı.")
            return 0
        defer_user(deferred, name, "manual_defer_after_unverified_click")
        save_deferred(digest, deferred)
        print(f"@{name} ertelendi. Instagram'a girilmedi; hiçbir düğmeye basılmadı.")
        print(f"Ertelenenler: {DEFERRED_PATH}")
        return 0

    if args.deferred_review or args.deferred_report or args.apply_deferred:
        return run_deferred_mode(args, users, digest, processed, deferred, reviews)

    # Dışa aktarımın sırasını koru. Belirsiz sonuçları 'cancelled' olarak gösterme.
    pending = [u for u in users if u.casefold() not in processed]
    active_deferred = [u for u in pending if u.casefold() in deferred]
    if args.retry_deferred:
        remaining = active_deferred
    else:
        remaining = [u for u in pending if u.casefold() not in deferred]
    print(
        f"Arşivde {len(users)} kişi bulundu | Önceden işlenen: {len(users) - len(pending)} "
        f"| Ertelenen: {len(active_deferred)} | Bu turda kalan: {len(remaining)}"
    )
    if args.status:
        cancelled = sum(state == "cancelled" for state in processed.values())
        no_pending = sum(state == "not_pending" for state in processed.values())
        print(f"Durum: iptal kaydı={cancelled} | bekleyen yok={no_pending} "
              f"| ertelenen={len(active_deferred)} | sırada={len(remaining)}")
        print(f"İlerleme: {PROGRESS_PATH}")
        print(f"Ertelenenler: {DEFERRED_PATH}")
        review_summary = deferred_review_summary(deferred, reviews)
        print(f"Deferred inceleme: incelenen={review_summary['reviewed']} "
              f"incelenmemiş={review_summary['unreviewed']} "
              f"manuel={review_summary['manual_review']} "
              f"incelemede_iptal={review_summary['resolved_cancelled']}")
        print("Salt okunur rapor: Firefox/Instagram açılmadı.")
        return 0
    if not args.apply and not args.inspect_user and not args.reconcile_user:
        for name in remaining[:30]:
            print(f"  @{name}")
        if len(remaining) > 30:
            print(f"  ... ve {len(remaining) - 30} kullanıcı daha")
        print("\nÖNİZLEME: Instagram'a hiçbir işlem yapılmadı. Başlatmak için --apply kullan.")
        return 0
    if not remaining and not args.inspect_user and not args.reconcile_user:
        print("Yapılacak işlem kalmadı.")
        return 0

    print(f"\nThe Firefox session will be verified as @{args.account}.")
    if args.apply and not args.non_interactive and input(
            "Type APPLY to authorize follow-request withdrawals: ").strip() != "APPLY":
        print("Cancelled without making changes.")
        return 0

    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    firefox = shutil.which("firefox")
    gecko = shutil.which("geckodriver")
    if not firefox or not gecko:
        print("HATA: Firefox/Geckodriver bulunamadı. Betiği Nix ortamında çalıştır.", file=sys.stderr)
        return 2
    PROFILE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    options = Options()
    options.binary_location = firefox
    options.add_argument("-profile")
    options.add_argument(str(PROFILE_DIR.resolve()))
    driver = None
    lock_handle = None
    completed_now, skipped_now, failures, unverified_now = 0, 0, 0, 0
    inaccessible_now, consecutive_inaccessible = 0, 0
    ambiguity_attempts: list[bool] = []
    safety_stop = False
    transient_failure = False
    try:
        if args.non_interactive:
            LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_handle = LOCK_PATH.open("a", encoding="utf-8")
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Başka bir istek temizleme süreci zaten çalışıyor; güvenli şekilde duruldu.")
        driver = start_verified_driver(webdriver, options, Service, gecko, args.account)
        print(f"Active account verified as @{args.account}.")
        if args.inspect_user or args.reconcile_user:
            name = args.inspect_user or args.reconcile_user
            driver.get(f"https://www.instagram.com/{name}/")
            guard_session(driver)
            try:
                state = wait_profile_state(driver, expected_user=name)
            except RuntimeError as exc:
                print(f"HATA: {exc}")
                return 1
            descriptions = {
                "requested": "İstek Gönderildi (hâlâ bekliyor)",
                "follow": "Takip Et (bekleyen istek yok)",
                "following": "Takiptesin (bekleyen istek yok)",
            }
            print(f"@{name}: {descriptions.get(state, 'Durum belirsiz')}")
            if args.reconcile_user and state in {"follow", "following"}:
                processed[name.casefold()] = "not_pending"
                save_progress(digest, processed)
                del deferred[name.casefold()]
                save_deferred(digest, deferred)
                audit_event(name, "not_pending", f"profile_state={state}")
                print("Bekleyen istek yok: ilerleme kaydına eklendi ve ertelemeden çıkarıldı.")
            elif args.reconcile_user:
                print("İstek hâlâ bekliyor: ilerlemeye dokunulmadı, ertelenmiş kaldı.")
            print("Kontrol tamamlandı, hiçbir düğmeye basılmadı.")
            return 0
        queue = remaining[: args.limit] if args.limit else remaining
        for idx, name in enumerate(queue, 1):
            if deadline and datetime.now(timezone.utc) >= deadline:
                print("Bitiş saati geldi; sıradaki hesaba geçilmedi.")
                break
            print(f"[{idx}/{len(queue)}] @{name}: ", end="", flush=True)
            try:
                result = cancel_one(driver, name)
                consecutive_inaccessible = 0
                ambiguity_attempts.append(False)
                processed[name.lower()] = result
                save_progress(digest, processed)
                audit_event(name, result, "profile_state=follow" if result == "cancelled" else "profile_state=follow_before_action")
                if name.casefold() in deferred:
                    del deferred[name.casefold()]
                    save_deferred(digest, deferred)
                if result == "cancelled":
                    completed_now += 1
                    print("İstek iptal edildi ✓")
                else:
                    skipped_now += 1
                    print("Artık bekleyen istek yok; dokunulmadı")
            except ProfileUnavailableError as exc:
                ambiguity_attempts.append(False)
                failures += 1
                inaccessible_now += 1
                consecutive_inaccessible += 1
                if args.defer_unverified and not args.retry_deferred:
                    defer_user(deferred, name, "profile_page_unavailable")
                    save_deferred(digest, deferred)
                    audit_event(name, "inaccessible", "explicit_unavailable_profile_message")
                    print(f"ERİŞİLEMİYOR — ERTELENDİ: {exc}")
                    print(f"İlerlemeye eklenmedi; ertelenenler: {DEFERRED_PATH}")
                    if consecutive_inaccessible >= 3:
                        print("DURDU: Art arda 3 profil erişilemez çıktı; yaygın erişim sorunu olabilir.")
                        break
                    if idx < len(queue):
                        time.sleep(max(20.0, args.max_delay))
                    continue
                print(f"DURDU: {exc}")
                break
            except RenderingTimeoutBeforeConfirmationClick as exc:
                unverified_now += 1
                ambiguity_attempts.append(True)
                try:
                    if exc.confirmation_clicked is not False:
                        raise RuntimeError(
                            "Render kurtarmasında onay tıklanmadığı kanıtlanamadı."
                        )
                    persist_render_timeout_defer(digest, usernames, deferred, name)
                except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as persist_exc:
                    failures += 1
                    safety_stop = True
                    print(f"DURDU: {persist_exc}")
                    break
                threshold = ambiguity_stop_reason(ambiguity_attempts)
                if threshold:
                    failures += 1
                    safety_stop = True
                    print(f"DURDU: {threshold}; @{name} ertelendi ancak yeni oturum açılmadı.")
                    break
                print(f"RENDER ZAMAN AŞIMI — GÜVENLE ERTELENDİ: {exc}")
                try:
                    close_driver_and_verify(driver)
                    driver = None
                    driver = start_verified_driver(
                        webdriver, options, Service, gecko, args.account)
                    audit_event(name, "session_restarted",
                                f"old_processes_exited; owner={args.account}")
                    print(f"Previous Firefox exited; new session verified as @{args.account}.")
                except Exception as recovery_exc:
                    failures += 1
                    safety_stop = True
                    audit_event(name, "failed", "render_recovery_session_failed")
                    print(f"DURDU: Firefox oturumu güvenle yenilenemedi: {recovery_exc}")
                    break
            except EmptyConfirmationDialogError as exc:
                ambiguity_attempts.append(True)
                unverified_now += 1
                failures += 1
                if args.defer_unverified:
                    defer_user(deferred, name, "empty_confirmation_dialog")
                    save_deferred(digest, deferred)
                    audit_event(name, "deferred", "empty_confirmation_dialog; cancellation_not_verified")
                    print(f"BOŞ ONAY PENCERESİ — ERTELENDİ; TÜM ÇALIŞMA DURDURULDU: {exc}")
                else:
                    audit_event(name, "failed", "empty_confirmation_dialog; cancellation_not_verified")
                    print(f"DURDU: {exc}")
                safety_stop = True
                break
            except AmbiguousConfirmationDialogError as exc:
                ambiguity_attempts.append(True)
                unverified_now += 1
                failures += 1
                if args.defer_unverified:
                    defer_user(deferred, name, "ambiguous_confirmation_not_clicked")
                    save_deferred(digest, deferred)
                    audit_event(name, "deferred", "ambiguous_confirmation; confirmation_not_clicked")
                    print(f"BELİRSİZ ONAY — ERTELENDİ; TÜM ÇALIŞMA DURDURULDU: {exc}")
                else:
                    audit_event(name, "failed", "ambiguous_confirmation; confirmation_not_clicked")
                    print(f"DURDU: {exc}")
                safety_stop = True
                break
            except AmbiguousCancellationError as exc:
                ambiguity_attempts.append(True)
                consecutive_inaccessible = 0
                unverified_now += 1
                failures += 1
                if args.defer_unverified:
                    defer_user(deferred, name, "click_done_but_not_verified")
                    save_deferred(digest, deferred)
                    audit_event(name, "deferred", "cancellation_not_verified")
                    print(f"BELİRSİZ İŞLEM — ERTELENDİ; TÜM ÇALIŞMA DURDURULDU: {exc}")
                else:
                    audit_event(name, "failed", "cancellation_not_verified")
                    print(f"DURDU: {exc}")
                safety_stop = True
                break
            except RuntimeError as exc:
                audit_event(name, "failed", "runtime_guard_or_ui_error")
                print(f"DURDU: {exc}")
                failures += 1
                safety_stop = True
                # Oturum/kısıtlama veya bilinmeyen UI: asla otomatik atlama.
                break
            except Exception as exc:
                log_unexpected_operational_error(name, exc)
                print(f"Hata: {type(exc).__name__}: {exc}")
                failures += 1
                transient_failure = True
                break
            if idx < len(queue):
                delay = random.uniform(args.min_delay, args.max_delay)
                if deadline:
                    delay = min(delay, max(0, (deadline - datetime.now(timezone.utc)).total_seconds()))
                time.sleep(delay)
                if idx % 20 == 0:
                    print("20 işlem tamamlandı; kısa ara...")
                    time.sleep(45)
    except KeyboardInterrupt:
        print("\nCtrl+C alındı; tamamlananlar kaydedildi.")
        transient_failure = True
    except RuntimeError as exc:
        print(f"GÜVENLİK DURUŞU: {exc}", file=sys.stderr)
        safety_stop = True
    except Exception as exc:
        print(f"GEÇİCİ ÇALIŞMA HATASI: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        transient_failure = True
    finally:
        if driver:
            try:
                close_driver_and_verify(driver)
            except Exception as exc:
                print(f"FIREFOX KAPATMA HATASI: {type(exc).__name__}: {exc}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                transient_failure = True
                failures += 1
        if lock_handle:
            lock_handle.close()
    print(
        f"\nBu tur: iptal={completed_now}, artık beklemiyor={skipped_now}, "
        f"belirsiz={unverified_now}, erişilemeyen={inaccessible_now}, hata={failures}"
    )
    print(f"İlerleme dosyası: {PROGRESS_PATH}")
    print(f"Ertelenenler: {DEFERRED_PATH} (işlem yapıldı sanılmıyor)")
    result_code = run_exit_code(safety_stop, transient_failure, failures)
    if result_code == EXIT_SAFETY_STOP:
        print(f"Çıkış kodu: {EXIT_SAFETY_STOP} (güvenlik nedeniyle durdu)")
        return result_code
    if result_code == EXIT_TRANSIENT:
        print(f"Çıkış kodu: {EXIT_TRANSIENT} (geçici/operasyonel hata)")
        return result_code
    print(f"Çıkış kodu: {EXIT_OK} (başarılı)")
    return result_code


if __name__ == "__main__":
    sys.exit(main())
