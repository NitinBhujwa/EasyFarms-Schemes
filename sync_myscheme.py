"""
EasyFarms myScheme sync — GitHub Actions edition.

Runs on a schedule inside GitHub Actions. No server, no Firestore.
Fetches scheme data from myScheme's search/detail/documents APIs and
upserts it into structured JSON files under data/. Writes a public
run report to reports/latest.json. The GitHub Actions workflow commits
and pushes any changed files back to this (public) repo.

SECURITY
--------
The only required credential is MYSCHEME_API_KEY, read from an
environment variable (set as a GitHub Actions secret). It is never
written to any file, printed, or logged — a logging filter redacts it
if it ever appears in a log line. TELEGRAM_BOT_TOKEN follows the same
rule. Do not hardcode credentials here; this file lives in a public
repo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# CONFIG
# ============================================================

APP_NAME = "easyfarms-myscheme-sync"
APP_VERSION = "3.0.0-actions"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

MYSCHEME_API_KEY = os.getenv("MYSCHEME_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SCHEME_CATEGORY = os.getenv("SCHEME_CATEGORY", "Agriculture,Rural & Environment")

SEARCH_PAGE_SIZE = int(os.getenv("SEARCH_PAGE_SIZE", "100"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.35"))

RECONCILE_REMOVED_SCHEMES = (
    os.getenv("RECONCILE_REMOVED_SCHEMES", "true").lower() == "true"
)

# This script assumes it runs from anywhere inside the repo checkout;
# all data paths are resolved relative to the repo root (one level up
# from this scripts/ directory).
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SCHEMES_DIR = DATA_DIR / "schemes"
RAW_DIR = DATA_DIR / "raw"
INDEX_PATH = DATA_DIR / "index.json"
REPORTS_DIR = REPO_ROOT / "reports"
REPORT_PATH = REPORTS_DIR / "latest.json"

SEARCH_API_URL = "https://api.myscheme.gov.in/search/v6/schemes"
DETAIL_API_URL = "https://api.myscheme.gov.in/schemes/v6/public/schemes"
DOCUMENT_API_URL_TEMPLATE = (
    "https://api.myscheme.gov.in/schemes/v6/public/schemes/{}/documents"
)


# ============================================================
# ENV VALIDATION
# ============================================================

def require_env(name: str, value: str | None) -> str:
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


MYSCHEME_API_KEY = require_env("MYSCHEME_API_KEY", MYSCHEME_API_KEY)


# ============================================================
# LOGGING (with secret redaction)
# ============================================================

class _RedactSecrets(logging.Filter):
    """Belt-and-braces: strip any accidental secret value from log lines."""

    def __init__(self, secrets_to_redact: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets_to_redact if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self._secrets:
            if secret in msg:
                msg = msg.replace(secret, "***REDACTED***")
        record.msg = msg
        record.args = ()
        return True


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(APP_NAME)
logger.addFilter(_RedactSecrets([MYSCHEME_API_KEY, TELEGRAM_BOT_TOKEN]))


# ============================================================
# TIME / JSON HELPERS
# ============================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read %s, using default", path)
        return default


def write_json(path: Path, value: Any) -> None:
    """Atomic write: write to a temp file, then replace. Avoids leaving
    a half-written file behind if the job is cancelled mid-run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        f.write("\n")
    tmp_path.replace(path)


# ============================================================
# HTTP SESSION
# ============================================================

def create_http_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(
        {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
            "cache-control": "no-cache",
            "origin": "https://www.myscheme.gov.in",
            "pragma": "no-cache",
            "referer": "https://www.myscheme.gov.in/",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
            ),
            "x-api-key": MYSCHEME_API_KEY,
        }
    )
    return session


# ============================================================
# SEARCH API
# ============================================================

def build_search_query() -> str:
    filters = [{"identifier": "schemeCategory", "value": SCHEME_CATEGORY}]
    return json.dumps(filters, ensure_ascii=False, separators=(",", ":"))


def fetch_search_page(session: requests.Session, start: int) -> dict[str, Any]:
    params = {
        "lang": "en",
        "q": build_search_query(),
        "keyword": "",
        "sort": "",
        "from": start,
        "size": SEARCH_PAGE_SIZE,
    }
    response = session.get(SEARCH_API_URL, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Search API returned non-object JSON.")
    return payload


def extract_search_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("data", {}).get("hits", {}).get("items", [])
    return items if isinstance(items, list) else []


def extract_search_total(payload: dict[str, Any]) -> int | None:
    try:
        return int(payload["data"]["summary"]["total"])
    except (KeyError, TypeError, ValueError):
        return None


# ============================================================
# DETAIL / DOCUMENTS API
# ============================================================

def fetch_scheme_detail(session: requests.Session, slug: str) -> dict[str, Any]:
    response = session.get(
        DETAIL_API_URL, params={"slug": slug, "lang": "en"}, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Detail API returned non-object JSON.")
    return payload


def fetch_scheme_documents(session: requests.Session, scheme_id: str) -> dict[str, Any]:
    url = DOCUMENT_API_URL_TEMPLATE.format(scheme_id)
    response = session.get(url, params={"lang": "en"}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Documents API returned non-object JSON.")
    return payload


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_scheme(
    search_item: dict[str, Any],
    detail_response: dict[str, Any],
    document_response: dict[str, Any] | None,
) -> dict[str, Any]:
    data = detail_response.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("Detail response missing data.")

    en = data.get("en", {})
    en = en if isinstance(en, dict) else {}

    basic = en.get("basicDetails", {})
    basic = basic if isinstance(basic, dict) else {}

    content = en.get("schemeContent", {})
    content = content if isinstance(content, dict) else {}

    eligibility = en.get("eligibilityCriteria", {})
    eligibility = eligibility if isinstance(eligibility, dict) else {}

    application_process = en.get("applicationProcess", [])

    scheme_id = data.get("_id")
    if not scheme_id:
        raise ValueError("Detail response missing _id.")

    slug = detail_response.get("slug")
    if not slug:
        fields = search_item.get("fields", {})
        slug = fields.get("slug") if isinstance(fields, dict) else None

    return {
        "scheme_id": str(scheme_id),
        "slug": slug,
        "scheme_name": basic.get("schemeName"),
        "scheme_short_title": basic.get("schemeShortTitle"),
        "implementing_agency": basic.get("implementingAgency"),
        "scheme_for": basic.get("schemeFor"),
        "level": basic.get("level"),
        "state": basic.get("state"),
        "nodal_department": basic.get("nodalDepartmentName"),
        "tags": basic.get("tags", []),
        "categories": basic.get("schemeCategory", []),
        "subcategories": basic.get("schemeSubCategory", []),
        "target_beneficiaries": basic.get("targetBeneficiaries", []),
        "scheme_open_date": basic.get("schemeOpenDate"),
        "scheme_close_date": basic.get("schemeCloseDate"),
        "dbt_scheme": basic.get("dbtScheme"),
        "brief_description": en.get("briefDescription"),
        "detailed_description": (
            content.get("detailedDescription_md") or en.get("detailedDescription_md")
        ),
        "benefit_type": content.get("benefitTypes") or en.get("benefitTypes"),
        "benefits": (
            content.get("benefits_md")
            or en.get("benefits_md")
            or en.get("benefits")
        ),
        "eligibility": (
            eligibility.get("eligibilityDescription_md")
            or eligibility.get("eligibilityDescription")
        ),
        "exclusions": (
            content.get("exclusions_md")
            or en.get("exclusions_md")
            or en.get("exclusions")
        ),
        "application_process": application_process,
        "required_documents": document_response,
        "references": content.get("references", []),
        "source": {
            "provider": "myScheme",
            "search_api": SEARCH_API_URL,
            "detail_api": DETAIL_API_URL,
            "documents_api": DOCUMENT_API_URL_TEMPLATE,
        },
        "content_hash": sha256_json(
            {"detail": detail_response, "documents": document_response}
        ),
    }


# ============================================================
# STORAGE (upsert into repo JSON files — replaces Firestore)
# ============================================================

def load_index() -> dict[str, Any]:
    return read_json(INDEX_PATH, default={})


def save_scheme_files(
    normalized: dict[str, Any],
    detail_response: dict[str, Any],
    document_response: dict[str, Any] | None,
    index: dict[str, Any],
) -> str:
    """Upsert one scheme's files. Returns 'added' | 'updated' | 'unchanged' |
    'reactivated'. Files are only rewritten when content actually changed,
    so unchanged schemes produce zero git diff on that run."""

    scheme_id = normalized["scheme_id"]
    now = utc_now_iso()
    existing_entry = index.get(scheme_id)
    new_hash = normalized["content_hash"]

    if existing_entry is None:
        status = "added"
    elif existing_entry.get("content_hash") != new_hash:
        status = "updated"
    elif not existing_entry.get("active", True):
        status = "reactivated"
    else:
        status = "unchanged"

    if status == "unchanged":
        return status

    first_seen_at = existing_entry.get("first_seen_at") if existing_entry else now

    if status in ("added", "updated"):
        payload = dict(normalized)
        payload.update(
            {
                "active": True,
                "first_seen_at": first_seen_at,
                "last_synced_at": now,
                "sync_source": "myScheme",
                "schema_version": "1.0",
            }
        )
        write_json(SCHEMES_DIR / f"{scheme_id}.json", payload)
        write_json(
            RAW_DIR / f"{scheme_id}.json",
            {
                "scheme_id": scheme_id,
                "fetched_at": now,
                "detail": detail_response,
                "documents": document_response,
            },
        )
        index[scheme_id] = {
            "scheme_id": scheme_id,
            "slug": normalized.get("slug"),
            "scheme_name": normalized.get("scheme_name"),
            "state": normalized.get("state"),
            "level": normalized.get("level"),
            "content_hash": new_hash,
            "active": True,
            "first_seen_at": first_seen_at,
            "last_synced_at": now,
        }
    else:  # reactivated — content identical to last sync, just flip active back on
        entry = dict(existing_entry)
        entry["active"] = True
        entry["last_synced_at"] = now
        index[scheme_id] = entry

        scheme_path = SCHEMES_DIR / f"{scheme_id}.json"
        scheme_data = read_json(scheme_path, default=None)
        if scheme_data is not None:
            scheme_data["active"] = True
            scheme_data.pop("deactivated_at", None)
            write_json(scheme_path, scheme_data)

    return status


def reconcile_removed(index: dict[str, Any], seen_ids: set[str]) -> int:
    """Soft-delete: schemes not seen in this run are marked inactive,
    never removed from the repo. Matches the existing 'never hard-delete
    temporarily-missing schemes' rule."""
    if not RECONCILE_REMOVED_SCHEMES:
        return 0

    now = utc_now_iso()
    deactivated = 0

    for scheme_id, entry in index.items():
        if scheme_id in seen_ids:
            continue
        if entry.get("active", True):
            entry["active"] = False
            entry["deactivated_at"] = now
            deactivated += 1

            scheme_path = SCHEMES_DIR / f"{scheme_id}.json"
            scheme_data = read_json(scheme_path, default=None)
            if scheme_data is not None:
                scheme_data["active"] = False
                scheme_data["deactivated_at"] = now
                write_json(scheme_path, scheme_data)

    return deactivated


# ============================================================
# TELEGRAM ALERTS (optional — only fires if secrets are configured)
# ============================================================

def send_telegram_alert(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception:
        logger.exception("Failed to send Telegram alert")


# ============================================================
# SYNC WORKFLOW
# ============================================================

def empty_stats() -> dict[str, int]:
    return {
        "search_pages": 0,
        "items_received": 0,
        "unique_schemes_found": 0,
        "detail_attempted": 0,
        "detail_success": 0,
        "detail_failed": 0,
        "documents_attempted": 0,
        "documents_success": 0,
        "documents_failed": 0,
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "reactivated": 0,
        "deactivated": 0,
        "total_failures": 0,
        "validation_failures": 0,
    }


def main() -> int:
    run_id = str(uuid.uuid4())
    started_monotonic = time.monotonic()
    started_at = utc_now_iso()

    index = load_index()
    stats = empty_stats()
    failed_schemes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    session = create_http_session()

    try:
        logger.info("SYNC STARTED: %s", run_id)

        start = 0
        reported_total: int | None = None

        while True:
            logger.info("Fetching search page: from=%s", start)
            payload = fetch_search_page(session, start)
            stats["search_pages"] += 1

            if reported_total is None:
                reported_total = extract_search_total(payload)

            items = extract_search_items(payload)
            stats["items_received"] += len(items)
            if not items:
                break

            for item in items:
                search_id = item.get("id")
                fields = item.get("fields", {})
                slug = fields.get("slug") if isinstance(fields, dict) else None

                if not search_id or not slug:
                    stats["validation_failures"] += 1
                    continue

                search_id = str(search_id)
                if search_id in seen_ids:
                    continue
                seen_ids.add(search_id)
                slug = str(slug)

                # ---------------- DETAIL ----------------
                stats["detail_attempted"] += 1
                try:
                    detail = fetch_scheme_detail(session, slug)
                    stats["detail_success"] += 1
                except Exception as exc:
                    stats["detail_failed"] += 1
                    stats["total_failures"] += 1
                    failed_schemes.append(
                        {"scheme_id": search_id, "slug": slug, "stage": "detail", "error": str(exc)}
                    )
                    continue

                detail_data = detail.get("data", {})
                if not isinstance(detail_data, dict) or not detail_data.get("_id"):
                    stats["validation_failures"] += 1
                    continue

                real_id = str(detail_data["_id"])

                # ---------------- DOCUMENTS ----------------
                stats["documents_attempted"] += 1
                documents = None
                try:
                    documents = fetch_scheme_documents(session, real_id)
                    stats["documents_success"] += 1
                except Exception as exc:
                    stats["documents_failed"] += 1
                    stats["total_failures"] += 1
                    failed_schemes.append(
                        {"scheme_id": real_id, "slug": slug, "stage": "documents", "error": str(exc)}
                    )

                # ---------------- NORMALIZE ----------------
                try:
                    normalized = normalize_scheme(item, detail, documents)
                except Exception as exc:
                    stats["validation_failures"] += 1
                    stats["total_failures"] += 1
                    failed_schemes.append(
                        {"scheme_id": real_id, "slug": slug, "stage": "normalize", "error": str(exc)}
                    )
                    continue

                # ---------------- UPSERT ----------------
                try:
                    status_word = save_scheme_files(normalized, detail, documents, index)
                    stats[status_word] += 1
                except Exception as exc:
                    stats["total_failures"] += 1
                    failed_schemes.append(
                        {"scheme_id": real_id, "slug": slug, "stage": "save", "error": str(exc)}
                    )

                if REQUEST_DELAY > 0:
                    time.sleep(REQUEST_DELAY)

            start += SEARCH_PAGE_SIZE
            if reported_total is not None and start >= reported_total:
                break
            if len(items) < SEARCH_PAGE_SIZE:
                break

        stats["unique_schemes_found"] = len(seen_ids)
        stats["deactivated"] = reconcile_removed(index, seen_ids)

        write_json(INDEX_PATH, index)

        duration = round(time.monotonic() - started_monotonic, 3)
        final_status = (
            "completed_with_errors"
            if (stats["total_failures"] or stats["validation_failures"])
            else "completed"
        )

        report = {
            "run_id": run_id,
            "app_version": APP_VERSION,
            "status": final_status,
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "duration_seconds": duration,
            "scheme_category": SCHEME_CATEGORY,
            "stats": stats,
            "failed_schemes": failed_schemes[:200],
            "failure_count": len(failed_schemes),
        }
        write_json(REPORT_PATH, report)

        logger.info("SYNC FINISHED: %s status=%s", run_id, final_status)

        if final_status == "completed_with_errors":
            send_telegram_alert(
                "⚠️ EasyFarms myScheme sync completed with errors\n"
                f"run_id: {run_id}\n"
                f"total_failures: {stats['total_failures']}, "
                f"validation_failures: {stats['validation_failures']}"
            )

        return 0

    except Exception as exc:
        duration = round(time.monotonic() - started_monotonic, 3)
        report = {
            "run_id": run_id,
            "app_version": APP_VERSION,
            "status": "failed",
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "duration_seconds": duration,
            "scheme_category": SCHEME_CATEGORY,
            "stats": stats,
            "failure": {"error": str(exc)},
        }
        write_json(REPORT_PATH, report)

        logger.error("SYNC FAILED: %s\n%s", run_id, traceback.format_exc())
        send_telegram_alert(
            f"🔴 EasyFarms myScheme sync FAILED\nrun_id: {run_id}\nerror: {exc}"
        )
        return 1

    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
