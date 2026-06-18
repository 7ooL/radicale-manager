import logging
import os
import json
import zipfile
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta
from io import BytesIO
import re
import uuid
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
    jsonify,
    Response,
)
from radicale_client import RadicaleClient
from contact_utils import (
    parse_vcf_contacts,
    parse_vcard_contact,
    vcard_to_dict,
    build_vcard_from_fields,
    duplicate_vcard,
    get_contact_filename,
    merge_unknown_fields_into_vcard,
)
from credential_store import CredentialStore, HAS_FERNET


def configure_logging(app):
    """Configure Flask app logging for debugging and traceability."""
    if not app.logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        )
        handler.setFormatter(formatter)
        app.logger.addHandler(handler)
    app.logger.setLevel(logging.DEBUG)


def humanize_timestamp(value):
    """Return a short relative time string for ISO-ish timestamps."""
    if not value:
        return "never"
    parsed = parse_iso_timestamp(value)
    if not parsed:
        return str(value)
    now = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = now - parsed
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if seconds < 604800:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''} ago"
    if seconds < 2592000:
        weeks = seconds // 604800
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    months = seconds // 2592000
    return f"{months} month{'s' if months != 1 else ''} ago"


def create_app():
    load_dotenv(override=True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("APP_SECRET_KEY") or os.urandom(24)
    app.config["PROFILE_STORE_PATH"] = os.environ.get(
        "PROFILE_STORE_PATH",
        "/data/profiles.db",
    )
    app.config["DEFAULT_RADICALE_URL"] = os.environ.get(
        "DEFAULT_RADICALE_URL",
        "https://radicale.example.test",
    )
    app.config["PROFILE_SECRET"] = os.environ.get("PROFILE_SECRET_KEY")
    app.config["APP_VERSION"] = os.environ.get("APP_VERSION", "0.1.0")
    app.config["GIT_COMMIT"] = os.environ.get("GIT_COMMIT")
    configure_logging(app)
    return app


app = create_app()
credential_store = CredentialStore(app.config["PROFILE_STORE_PATH"], app.config["PROFILE_SECRET"])

# record process start for uptime
START_TIME = datetime.now(timezone.utc)


@app.template_filter("relative_time")
def relative_time_filter(value):
    return humanize_timestamp(value)

# Navigation registry: central place to declare visible pages
NAV_ITEMS = [
    {"name": "Dashboard", "endpoint": "dashboard", "icon": "🏠", "quick": True},
    {"name": "All Contacts", "endpoint": "global_contacts", "icon": "👥", "quick": True},
    {"name": "Address Books", "endpoint": "address_books", "icon": "📚", "quick": False},
    {"name": "Import", "endpoint": "import_vcf", "icon": "⬆️", "quick": False},
    {"name": "Settings", "endpoint": "system_menu", "icon": "⚙️", "quick": False},
]

NAV_TABS = [
    {"name": "Home", "endpoint": "dashboard", "icon": "🏠"},
    {"name": "Contacts", "endpoint": "global_contacts", "icon": "👥"},
    {"name": "Books", "endpoint": "address_books", "icon": "📚"},
    {"name": "Settings", "endpoint": "system_menu", "icon": "⚙️"},
]

EVENT_ACTION_LABELS = {
    "create": "Created contact",
    "edit": "Edited contact",
    "delete": "Deleted contact",
    "move": "Moved contact",
    "copy": "Copied contact",
    "duplicate": "Duplicated contact",
    "import": "Imported contacts",
    "export": "Exported contacts",
    "profile_export": "Exported profiles",
    "profile_import": "Imported profiles",
    "connection_test": "Tested connection",
    "addressbook_refresh": "Refreshed address books",
    "addressbook_export": "Exported address book",
    "contact_export": "Exported contact",
    "connection_export": "Exported connection",
    "backup_export": "Exported backup ZIP",
    "quality_scan": "Scanned contact quality",
    "quality_action": "Queued quality action",
    "quality_action_status": "Updated quality action status",
}

QUALITY_DUPLICATE_ACTIONS = {
    "ignore": "Ignore",
    "review": "Mark for review",
    "recommend_merge": "Recommend merge",
}

QUALITY_ACTION_STATUSES = {
    "queued": "Ready",
    "in_review": "In Review",
    "resolved": "Mitigated",
    "dismissed": "Dismissed",
}

QUALITY_STATUS_DESCRIPTIONS = {
    "queued": "Ready for triage and mitigation planning.",
    "in_review": "Actively being reviewed for a mitigation decision.",
    "resolved": "Mitigation applied or completed; no further action needed.",
    "dismissed": "Accepted risk / false positive; intentionally closed.",
}


def get_active_mobile_tab(endpoint):
    if endpoint in ("dashboard",):
        return "dashboard"
    if endpoint in (
        "global_contacts",
        "global_contact_quality",
        "profile_view_contacts",
        "profile_view_contact",
        "profile_contact_transfer",
        "profile_new_contact",
        "profile_edit_contact",
        "profile_contact_quality",
        "profile_copy_contact",
        "profile_move_contact",
        "profile_duplicate_contact",
        "profile_delete_contact",
        "profile_bulk_contacts",
        "global_bulk_contacts",
    ):
        return "global_contacts"
    if endpoint in ("address_books", "profile_import_vcf", "profile_export_addressbook"):
        return "address_books"
    if endpoint in (
        "connections",
        "connection_detail",
        "new_connection",
        "edit_connection",
        "delete_connection",
        "test_connection",
        "refresh_addressbooks",
        "create_addressbook",
        "rename_addressbook",
        "delete_addressbook",
        "export_connections",
        "import_connections",
        "profile_export_connection",
        "profile_backup_export",
        "system_events",
        "quality_review_queue",
        "quality_review_queue_merge_preview",
        "quality_review_queue_merge_apply",
    ):
        return "system_menu"
    if endpoint in ("system_menu", "system_routes", "health", "ready", "debug_profiles", "security_settings", "system_events", "quality_review_queue", "quality_review_queue_merge_preview", "quality_review_queue_merge_apply"):
        return "system_menu"
    return "dashboard"

def build_navigation(current_endpoint=None):
    nav = []
    for item in NAV_ITEMS:
        entry = dict(item)
        entry["active"] = item.get("endpoint") == current_endpoint
        entry["url"] = url_for(item["endpoint"])
        nav.append(entry)
    return nav

def build_breadcrumbs(endpoint, view_args):
    if not endpoint or endpoint == "dashboard":
        return []

    crumbs = [{"name": "Dashboard", "url": url_for("dashboard")}]
    view_args = view_args or {}

    profile_id = view_args.get("profile_id")
    collection_path = view_args.get("collection_path")
    if profile_id and not collection_path and endpoint == "connection_detail":
        profile = credential_store.get_profile(profile_id)
        crumbs.append({"name": "Settings", "url": url_for("system_menu")})
        crumbs.append({"name": "Connections", "url": url_for("connections")})
        crumbs.append({"name": profile.get("name") if profile else "Connection", "url": None})
        return crumbs
    if endpoint == "security_settings":
        crumbs.append({"name": "Settings", "url": url_for("system_menu")})
        crumbs.append({"name": "Credential Security", "url": None})
        return crumbs
    if endpoint == "system_events":
        crumbs.append({"name": "Settings", "url": url_for("system_menu")})
        crumbs.append({"name": "Event Log", "url": None})
        return crumbs
    if endpoint == "quality_review_queue":
        crumbs.append({"name": "Settings", "url": url_for("system_menu")})
        crumbs.append({"name": "Quality Review Queue", "url": None})
        return crumbs
    if endpoint == "quality_review_queue_merge_preview":
        crumbs.append({"name": "Settings", "url": url_for("system_menu")})
        crumbs.append({"name": "Quality Review Queue", "url": url_for("quality_review_queue")})
        crumbs.append({"name": "Merge Preview", "url": None})
        return crumbs
    if endpoint == "global_contact_quality":
        crumbs.append({"name": "All Contacts", "url": url_for("global_contacts")})
        crumbs.append({"name": "Global Quality Scan", "url": None})
        return crumbs
    if profile_id and collection_path:
        try:
            profile = credential_store.get_profile(profile_id)
            books = credential_store.get_cached_address_books(profile_id) or []
            book = normalize_book_for_template(
                next((b for b in books if b["path"] == collection_path), None),
                fallback_path=collection_path,
            )
            if profile:
                crumbs.append({"name": profile["name"], "url": url_for("dashboard")})
            crumbs.append(
                {
                    "name": book["display_name"],
                    "url": url_for(
                        "profile_view_contacts",
                        profile_id=profile_id,
                        collection_path=collection_path,
                    ),
                }
            )
            if endpoint == "profile_view_contact":
                crumbs.append({"name": "Contact", "url": None})
            elif endpoint == "profile_new_contact":
                crumbs.append({"name": "New Contact", "url": None})
            elif endpoint == "profile_edit_contact":
                crumbs.append({"name": "Edit Contact", "url": None})
            elif endpoint == "profile_import_vcf":
                crumbs.append({"name": "Import", "url": None})
            elif endpoint == "profile_contact_quality":
                crumbs.append({"name": "Quality Scan", "url": None})
            elif endpoint == "profile_contact_transfer":
                crumbs.append({"name": "Move / Copy", "url": None})
            if crumbs[-1]["url"]:
                crumbs[-1]["url"] = None
            return crumbs
        except Exception:
            app.logger.debug("Unable to build profile breadcrumb", exc_info=True)

    for item in NAV_ITEMS:
        if item["endpoint"] == endpoint:
            crumbs.append({"name": item["name"], "url": None})
            return crumbs
    return crumbs


@app.context_processor
def inject_navigation():
    # Provide navigation, quick actions, and breadcrumbs to all templates
    current_endpoint = request.endpoint
    navigation = build_navigation(current_endpoint)
    quick_actions = [i for i in NAV_ITEMS if i.get("quick")]
    breadcrumbs = build_breadcrumbs(current_endpoint, request.view_args or {})
    # small status summary
    try:
        total_profiles = len(credential_store.get_profiles())
    except Exception:
        total_profiles = 0
    try:
        total_books = sum(len(credential_store.get_cached_address_books(p["id"])) for p in credential_store.get_profiles())
    except Exception:
        total_books = 0
    status = {
        "app_version": app.config.get("APP_VERSION"),
        "profiles": total_profiles,
        "address_books": total_books,
    }
    active_tab = get_active_mobile_tab(current_endpoint)
    nav_tabs = []
    for tab in NAV_TABS:
        entry = dict(tab)
        entry["url"] = url_for(tab["endpoint"])
        entry["active"] = tab["endpoint"] == active_tab
        nav_tabs.append(entry)
    return {
        "navigation": navigation,
        "quick_actions": quick_actions,
        "breadcrumbs": breadcrumbs,
        "global_status": status,
        "nav_tabs": nav_tabs,
        "active_mobile_tab": active_tab,
    }


def get_client_for_profile(profile_id):
    """Load the profile and return an authenticated RadicaleClient or raise."""
    profile = credential_store.get_profile(profile_id)
    if not profile:
        raise ValueError(f"Profile not found: {profile_id}")
    if not profile.get("password"):
        raise ValueError("Profile has no stored password")
    return RadicaleClient(profile["server_url"], profile["username"], profile["password"])


def get_active_connection():
    """Return the currently active Radicale connection stored in session."""
    active = session.get("active_connection")
    app.logger.debug("Retrieved active connection: %s", bool(active))
    return active


def set_active_connection(server_url, username, password):
    """Save Radicale connection details in the user session."""
    session["active_connection"] = {
        "server_url": server_url,
        "username": username,
        "password": password,
    }
    app.logger.debug("Stored active connection for server: %s user: %s", server_url, username)


def clear_active_connection():
    """Remove the active connection from session state."""
    session.pop("active_connection", None)
    app.logger.debug("Cleared active connection from session")


def normalize_book_for_template(book, fallback_path=None):
    """Return address book data with both display_name and name keys for templates."""
    book = dict(book or {})
    path = book.get("path") or fallback_path
    display_name = (
        book.get("display_name")
        or book.get("displayname")
        or book.get("name")
        or path
    )
    book["path"] = path
    book["display_name"] = display_name
    book["name"] = display_name
    return book


def update_cached_contact_count(profile_id, collection_path, count):
    books = credential_store.get_cached_address_books(profile_id) or []
    book = next((b for b in books if b["path"] == collection_path), None)
    if not book:
        return
    updated = dict(book)
    updated["contact_count"] = count
    credential_store.save_or_update_address_books(profile_id, [updated])


def fill_missing_contact_counts(profile_id, client, books):
    updated_books = []
    for book in books:
        normalized = normalize_book_for_template(book)
        if normalized.get("contact_count") is None and normalized.get("path"):
            try:
                normalized["contact_count"] = len(client.list_contacts(normalized["path"]))
                credential_store.save_or_update_address_books(profile_id, [normalized])
            except Exception as exc:
                app.logger.warning(
                    "Unable to count contacts for profile %s book %s: %s",
                    profile_id,
                    normalized.get("path"),
                    exc,
                )
        updated_books.append(normalized)
    return updated_books


def build_import_targets():
    targets = []
    for profile in credential_store.get_enabled_profiles():
        books = credential_store.get_cached_address_books(profile["id"]) or []
        for book in books:
            normalized = normalize_book_for_template(book)
            targets.append(
                {
                    "profile_id": profile["id"],
                    "profile_name": profile["name"],
                    "path": normalized["path"],
                    "display_name": normalized["display_name"],
                    "value": f"{profile['id']}::{normalized['path']}",
                    "label": f"{profile['name']} / {normalized['display_name']}",
                }
            )
    return targets


def build_contact_destinations(exclude_profile_id=None, exclude_collection_path=None):
    destinations = []
    exclude_collection_path = exclude_collection_path.strip("/") if exclude_collection_path else None
    for profile in credential_store.get_enabled_profiles():
        books = credential_store.get_cached_address_books(profile["id"]) or []
        for book in books:
            normalized = normalize_book_for_template(book)
            if (
                exclude_profile_id == profile["id"]
                and exclude_collection_path
                and normalized["path"].strip("/") == exclude_collection_path
            ):
                continue
            destinations.append(
                {
                    "profile_id": profile["id"],
                    "profile_name": profile["name"],
                    "path": normalized["path"],
                    "display_name": normalized["display_name"],
                    "value": f"{profile['id']}::{normalized['path']}",
                    "display": f"{profile['name']} / {normalized['display_name']}",
                }
            )
    return destinations


def import_vcf_into_book(profile_id, collection_path, file_storage):
    if not file_storage:
        raise ValueError("Please choose a VCF file to import.")

    profile = credential_store.get_profile(profile_id)
    if not profile:
        raise ValueError("Profile not found.")
    if not profile.get("password"):
        raise ValueError("Selected profile has no stored password.")

    content = file_storage.read().decode("utf-8", errors="replace")
    contacts, failed = parse_vcf_contacts(content)
    client = RadicaleClient(profile["server_url"], profile["username"], profile["password"])

    created = updated = 0
    imported = []
    for contact in contacts:
        try:
            response = client.put_contact(collection_path, contact["filename"], contact["vcard"])
            if response.status_code == 201:
                created += 1
                status = "created"
            else:
                updated += 1
                status = "updated"
            imported.append(
                {
                    "name": contact.get("display_name") or contact["filename"],
                    "filename": contact["filename"],
                    "status": status,
                }
            )
        except Exception as exc:
            failed.append({"error": str(exc), "filename": contact.get("filename")})

    update_cached_contact_count(profile_id, collection_path, None)
    log_event(
        "import",
        profile_id=profile_id,
        collection_path=collection_path,
        details={"processed": len(contacts), "created": created, "updated": updated, "failed": len(failed)},
    )
    return {
        "processed": len(contacts),
        "created": created,
        "updated": updated,
        "failed": failed,
        "imported": imported,
    }


def slugify_addressbook_name(name):
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return slug or "addressbook"


def normalize_duplicate_key(value):
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def normalize_phone_key(value):
    return re.sub(r"\D+", "", value or "")


DEPRECATED_VCARD_FIELDS = {
    "AGENT": "AGENT is deprecated in modern vCard versions. Store assistant/contact links using supported custom fields.",
    "CLASS": "CLASS is deprecated. Remove it and use system-level access controls instead.",
    "LABEL": "LABEL is deprecated. Keep address display text in ADR components or NOTE.",
    "MAILER": "MAILER is deprecated. Remove it because client apps can infer source without this property.",
}


def _unfold_vcard_lines(raw_text):
    lines = []
    for line in (raw_text or "").replace("\r\n", "\n").split("\n"):
        if not line:
            continue
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _extract_vcard_property_names(raw_text):
    names = []
    for line in _unfold_vcard_lines(raw_text):
        if ":" not in line:
            continue
        names.append(line.split(":", 1)[0].split(";", 1)[0].strip().upper())
    return names


def analyze_contact_quality_warnings(contact):
    raw_property_names = _extract_vcard_property_names(contact.get("raw") or "")
    deprecated_fields = sorted(
        {
            property_name
            for property_name in raw_property_names
            if property_name in DEPRECATED_VCARD_FIELDS
        }
    )
    empty_contact = not any(
        [
            (contact.get("full_name") or "").strip(),
            (contact.get("first_name") or "").strip(),
            (contact.get("last_name") or "").strip(),
            (contact.get("nickname") or "").strip(),
            (contact.get("organization") or "").strip(),
            (contact.get("job_title") or "").strip(),
            (contact.get("birthday") or "").strip(),
            (contact.get("address") or "").strip(),
            (contact.get("note") or "").strip(),
            contact.get("emails") or [],
            contact.get("phones") or [],
            contact.get("urls") or [],
            contact.get("categories") or [],
        ]
    )

    warnings = []
    recommendations = []
    if empty_contact:
        recommendation = "Add a name and at least one contact method (email or phone), or remove this placeholder entry."
        warnings.append(
            {
                "type": "Empty contact",
                "detail": "No meaningful profile fields were found.",
                "recommendation": recommendation,
            }
        )
        recommendations.append(recommendation)
    if deprecated_fields:
        recommendation = "Replace deprecated fields with modern equivalents and re-save the contact."
        warnings.append(
            {
                "type": "Deprecated fields",
                "detail": ", ".join(deprecated_fields),
                "recommendation": recommendation,
            }
        )
        recommendations.append(recommendation)
    return {
        "empty_contact": empty_contact,
        "deprecated_fields": deprecated_fields,
        "warnings": warnings,
        "recommendations": recommendations,
    }


def assess_contact_health(contact, analysis=None):
    analysis = analysis or analyze_contact_quality_warnings(contact)
    missing = []
    emails = contact.get("emails") or []
    phones = contact.get("phones") or []
    if not contact.get("full_name"):
        missing.append("name")
    if not emails:
        missing.append("email")
    if not phones:
        missing.append("phone")

    score = 100
    if "name" in missing:
        score -= 35
    if "email" in missing:
        score -= 25
    if "phone" in missing:
        score -= 25
    if not emails and not phones:
        score -= 10
    if analysis.get("empty_contact"):
        score -= 35
    if analysis.get("deprecated_fields"):
        score -= min(20, 10 * len(analysis["deprecated_fields"]))
    score = max(0, min(100, score))

    if score >= 90:
        label = "Excellent"
        tone = "excellent"
    elif score >= 75:
        label = "Good"
        tone = "good"
    elif score >= 55:
        label = "Fair"
        tone = "fair"
    else:
        label = "Risk"
        tone = "risk"

    return {
        "score": score,
        "label": label,
        "tone": tone,
        "missing": missing,
        "empty_contact": analysis.get("empty_contact", False),
        "deprecated_fields": analysis.get("deprecated_fields", []),
        "warnings": analysis.get("warnings", []),
        "recommendations": analysis.get("recommendations", []),
    }


def build_duplicate_report(contacts):
    checks = {"email": {}, "phone": {}, "name": {}}
    issues = []
    warnings = []
    score_sum = 0
    score_count = 0
    low_health_count = 0
    warning_contact_count = 0
    score_buckets = {"excellent": 0, "good": 0, "fair": 0, "risk": 0}
    scored_contacts = []

    similar_name_candidates = []
    for contact in contacts:
        display = contact.get("full_name") or contact.get("filename") or "Unnamed contact"
        analysis = analyze_contact_quality_warnings(contact)
        health = assess_contact_health(contact, analysis)
        entry = {
            "display": display,
            "filename": contact.get("filename"),
            "profile_id": contact.get("profile_id"),
            "email": ", ".join(contact.get("emails") or []),
            "phone": ", ".join(contact.get("phones") or []),
            "profile_name": contact.get("profile_name"),
            "book_name": contact.get("book_name"),
            "book_path": contact.get("book_path"),
            "quality_score": health["score"],
            "quality_label": health["label"],
            "quality_tone": health["tone"],
            "empty_contact": health["empty_contact"],
            "deprecated_fields": health["deprecated_fields"],
            "recommendations": health["recommendations"],
        }
        score_sum += health["score"]
        score_count += 1
        score_buckets[health["tone"]] = score_buckets.get(health["tone"], 0) + 1
        if health["score"] < 75:
            low_health_count += 1
        if health["warnings"]:
            warning_contact_count += 1
        scored_contacts.append(entry)
        for email in contact.get("emails") or []:
            key = normalize_duplicate_key(email)
            if key:
                checks["email"].setdefault(key, []).append(entry)
        for phone in contact.get("phones") or []:
            key = normalize_phone_key(phone)
            if key and len(key) >= 7:
                checks["phone"].setdefault(key, []).append(entry)
        name_key = normalize_duplicate_key(contact.get("full_name"))
        if name_key:
            checks["name"].setdefault(name_key, []).append(entry)
            similar_name_candidates.append({"name_key": name_key, "entry": entry})

        missing = health["missing"]
        if missing:
            missing_recommendation = (
                f"Add missing {' and '.join(missing)} values to improve contact completeness."
            )
            issues.append({**entry, "missing": missing, "recommendation": missing_recommendation})
        for warning in health["warnings"]:
            warnings.append({**entry, **warning})

    duplicate_groups = []

    def confidence_from_score(score):
        if score >= 90:
            return "High"
        if score >= 80:
            return "Medium"
        return "Low"

    def score_for_match_type(match_type, default_score=100):
        if match_type == "Name":
            return 92
        if match_type == "Phone":
            return 97
        if match_type == "Email":
            return 100
        return default_score
    for match_type, values in checks.items():
        for value, matches in values.items():
            if len(matches) > 1:
                duplicate_groups.append(
                    {
                        "type": match_type.title(),
                        "value": value,
                        "count": len(matches),
                        "contacts": matches,
                        "match_score": score_for_match_type(match_type.title()),
                        "confidence": confidence_from_score(score_for_match_type(match_type.title())),
                    }
                )

    # Add similar-name candidate groups (non-exact) using fuzzy ratio.
    seen_pairs = set()
    for index, left in enumerate(similar_name_candidates):
        for right in similar_name_candidates[index + 1 :]:
            left_key = left["name_key"]
            right_key = right["name_key"]
            if not left_key or not right_key or left_key == right_key:
                continue
            if len(left_key) < 4 or len(right_key) < 4:
                continue
            pair_key = tuple(sorted((left_key, right_key)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            similarity = SequenceMatcher(None, left_key, right_key).ratio()
            if similarity < 0.84:
                continue
            score = int(round(similarity * 100))
            duplicate_groups.append(
                {
                    "type": "Name Similarity",
                    "value": f"{left['entry']['display']} ~ {right['entry']['display']}",
                    "count": 2,
                    "contacts": [left["entry"], right["entry"]],
                    "match_score": score,
                    "confidence": confidence_from_score(score),
                }
            )

    duplicate_groups.sort(key=lambda group: (-group.get("match_score", 0), -group["count"], group["type"], group["value"]))
    for idx, group in enumerate(duplicate_groups, start=1):
        safe_type = re.sub(r"[^a-z0-9]+", "-", group.get("type", "").casefold()).strip("-")
        safe_value = re.sub(r"[^a-z0-9]+", "-", str(group.get("value", "")).casefold()).strip("-")
        group["group_id"] = f"{safe_type or 'group'}-{safe_value[:36] or 'value'}-{idx}"
    scored_contacts.sort(key=lambda item: (item["quality_score"], item["display"].casefold()))
    average_score = int(round(score_sum / score_count)) if score_count else 0
    return {
        "duplicates": duplicate_groups,
        "issues": issues,
        "warnings": warnings,
        "score": {
            "average": average_score,
            "contacts_scored": score_count,
            "low_health": low_health_count,
            "contacts_with_warnings": warning_contact_count,
            "excellent": score_buckets.get("excellent", 0),
            "good": score_buckets.get("good", 0),
            "fair": score_buckets.get("fair", 0),
            "risk": score_buckets.get("risk", 0),
            "lowest_contacts": scored_contacts[:8],
        },
    }


def quality_queue_key_from_values(scope, profile_id, collection_path, duplicate_type, duplicate_value, queue_action):
    return "|".join(
        [
            str(scope or "global"),
            str(profile_id or ""),
            str(collection_path or ""),
            str(duplicate_type or ""),
            str(duplicate_value or ""),
            str(queue_action or ""),
        ]
    )


def quality_queue_key_from_event(event):
    details = event.get("details") or {}
    return quality_queue_key_from_values(
        details.get("scope"),
        event.get("profile_id") or details.get("profile_id"),
        event.get("collection_path") or details.get("collection_path"),
        details.get("duplicate_type"),
        details.get("duplicate_value"),
        details.get("action"),
    )


def build_quality_queue_entries(limit=1000):
    action_events = credential_store.get_recent_events(limit=limit, actions=["quality_action"])
    status_events = credential_store.get_recent_events(limit=limit, actions=["quality_action_status"])
    profiles = credential_store.get_profiles()
    profile_names = {profile["id"]: profile["name"] for profile in profiles}

    latest_status_by_key = {}
    for event in status_events:
        details = event.get("details") or {}
        queue_key = details.get("queue_key")
        if not queue_key or queue_key in latest_status_by_key:
            continue
        latest_status_by_key[queue_key] = {
            "status": details.get("status") or "queued",
            "updated_at": event.get("created_at"),
            "details": details,
        }

    queue_entries = {}
    for event in action_events:
        details = event.get("details") or {}
        queue_key = details.get("queue_key") or quality_queue_key_from_event(event)
        if not queue_key or queue_key in queue_entries:
            continue
        status_info = latest_status_by_key.get(queue_key) or {}
        profile_id = event.get("profile_id")
        duplicate_contacts = details.get("duplicate_contacts") or []
        if isinstance(duplicate_contacts, str):
            try:
                duplicate_contacts = json.loads(duplicate_contacts)
            except Exception:
                duplicate_contacts = []
        if not isinstance(duplicate_contacts, list):
            duplicate_contacts = []
        normalized_contacts = []
        for item in duplicate_contacts[:4]:
            if isinstance(item, dict):
                normalized_contacts.append(
                    {
                        "display": item.get("display") or "",
                        "email": item.get("email") or "",
                        "phone": item.get("phone") or "",
                        "profile_name": item.get("profile_name") or "",
                        "book_name": item.get("book_name") or "",
                        "profile_id": item.get("profile_id"),
                        "book_path": item.get("book_path") or "",
                        "filename": item.get("filename") or "",
                    }
                )
        actionable_contacts = [
            item for item in normalized_contacts
            if item.get("profile_id") and item.get("book_path") and item.get("filename")
        ]
        merged_preview = {}
        if normalized_contacts:
            names = [item.get("display") for item in normalized_contacts if item.get("display")]
            emails = sorted({part.strip() for item in normalized_contacts for part in (item.get("email") or "").split(",") if part.strip()})
            phones = sorted({part.strip() for item in normalized_contacts for part in (item.get("phone") or "").split(",") if part.strip()})
            merged_preview = {
                "display": names[0] if names else "Merged contact",
                "emails": emails,
                "phones": phones,
            }
        queue_entries[queue_key] = {
            "queue_key": queue_key,
            "scope": details.get("scope") or "global",
            "action": details.get("action") or "",
            "action_label": QUALITY_DUPLICATE_ACTIONS.get(details.get("action"), details.get("label") or (details.get("action") or "").title()),
            "duplicate_type": details.get("duplicate_type") or "",
            "duplicate_value": details.get("duplicate_value") or "",
            "match_score": details.get("match_score") or "",
            "confidence": details.get("confidence") or "",
            "profile_id": profile_id,
            "profile_name": profile_names.get(profile_id) if profile_id else "All profiles",
            "collection_path": event.get("collection_path") or details.get("collection_path") or "",
            "created_at": event.get("created_at"),
            "status": status_info.get("status") or details.get("status") or "queued",
            "status_updated_at": status_info.get("updated_at") or event.get("created_at"),
            "status_details": status_info.get("details") or {},
            "duplicate_contacts": normalized_contacts,
            "actionable_contact_count": len(actionable_contacts),
            "can_preview_merge": bool((details.get("action") or "") == "recommend_merge" and len(actionable_contacts) >= 2),
            "merged_preview": merged_preview,
        }
    return queue_entries


def attach_queue_stage_to_duplicate_groups(duplicate_groups, scope, profile_id=None, collection_path=""):
    queue_entries = build_quality_queue_entries(limit=1000)
    normalized_path = (collection_path or "").strip("/")
    for group in duplicate_groups or []:
        action_states = {}
        for action_key in QUALITY_DUPLICATE_ACTIONS:
            queue_key = quality_queue_key_from_values(
                scope,
                profile_id,
                normalized_path,
                group.get("type"),
                group.get("value"),
                action_key,
            )
            entry = queue_entries.get(queue_key)
            if not entry:
                continue
            status = entry.get("status") or "queued"
            action_states[action_key] = {
                "status": status,
                "label": QUALITY_ACTION_STATUSES.get(status, status.replace("_", " ").title()),
                "queue_key": queue_key,
                "can_preview_merge": bool(entry.get("can_preview_merge")),
            }
        group["queue_state_by_action"] = action_states
    return duplicate_groups


def duplicate_contact_matches(entry, contact):
    duplicate_type = (entry.get("duplicate_type") or "").strip().casefold()
    duplicate_value = (entry.get("duplicate_value") or "").strip()
    if not duplicate_type or not duplicate_value:
        return False

    if duplicate_type == "email":
        target = normalize_duplicate_key(duplicate_value)
        if not target:
            return False
        for email in contact.get("emails") or []:
            if normalize_duplicate_key(email) == target:
                return True
        return False

    if duplicate_type == "phone":
        target = normalize_phone_key(duplicate_value)
        if not target:
            return False
        for phone in contact.get("phones") or []:
            if normalize_phone_key(phone) == target:
                return True
        return False

    if duplicate_type == "name":
        target = normalize_duplicate_key(duplicate_value)
        if not target:
            return False
        return normalize_duplicate_key(contact.get("full_name")) == target

    if duplicate_type == "name similarity":
        parts = [part.strip() for part in duplicate_value.split("~", 1)]
        if len(parts) != 2:
            return False
        candidate_names = {normalize_duplicate_key(parts[0]), normalize_duplicate_key(parts[1])}
        display = normalize_duplicate_key(contact.get("full_name") or contact.get("filename"))
        return bool(display and display in candidate_names)

    return False


def rebuild_duplicate_contacts_for_queue_entry(entry, max_contacts=2):
    scope = (entry.get("scope") or "global").strip()
    target_profile_id = entry.get("profile_id")
    target_book = (entry.get("collection_path") or "").strip("/")

    if scope == "book" and (not target_profile_id or not target_book):
        return []

    source_profiles = []
    if scope == "book":
        profile = credential_store.get_profile(int(target_profile_id)) if target_profile_id else None
        if profile:
            source_profiles = [profile]
    else:
        source_profiles = credential_store.get_enabled_profiles()

    matches = []
    for profile in source_profiles:
        profile_id = profile.get("id")
        if not profile_id:
            continue
        if scope == "global" and target_profile_id and int(target_profile_id) != int(profile_id):
            continue
        try:
            client = get_client_for_profile(int(profile_id))
        except Exception:
            continue
        books = credential_store.get_cached_address_books(int(profile_id)) or []
        for book in books:
            normalized_book = normalize_book_for_template(book)
            normalized_path = normalized_book["path"].strip("/")
            if scope == "book" and normalized_path != target_book:
                continue
            try:
                listed = client.list_contacts(normalized_book["path"])
            except Exception:
                continue
            for item in listed:
                try:
                    parsed = vcard_to_dict(item["vcard"])
                except Exception:
                    continue
                if not duplicate_contact_matches(entry, parsed):
                    continue
                filename = get_contact_filename(item.get("href") or "")
                matches.append(
                    {
                        "display": parsed.get("full_name") or filename or "Unnamed contact",
                        "email": ", ".join(parsed.get("emails") or []),
                        "phone": ", ".join(parsed.get("phones") or []),
                        "profile_name": profile.get("name") or "",
                        "book_name": normalized_book["display_name"] or "",
                        "profile_id": int(profile_id),
                        "book_path": normalized_path,
                        "filename": filename,
                    }
                )
                if len(matches) >= max_contacts:
                    return matches
    return matches


def hydrate_queue_entry_contact_refs(entry):
    if not entry or (entry.get("action") != "recommend_merge"):
        return entry
    if entry.get("can_preview_merge"):
        return entry
    rebuilt = rebuild_duplicate_contacts_for_queue_entry(entry, max_contacts=2)
    if len(rebuilt) < 2:
        return entry

    details = {
        "action": entry.get("action"),
        "label": QUALITY_DUPLICATE_ACTIONS.get(entry.get("action"), entry.get("action_label") or "Recommend merge"),
        "scope": entry.get("scope") or "global",
        "duplicate_type": entry.get("duplicate_type") or "",
        "duplicate_value": entry.get("duplicate_value") or "",
        "match_score": entry.get("match_score") or "",
        "confidence": entry.get("confidence") or "",
        "status": entry.get("status") or "queued",
        "queue_key": entry.get("queue_key"),
        "duplicate_contacts": rebuilt,
    }
    log_event(
        "quality_action",
        profile_id=entry.get("profile_id"),
        collection_path=entry.get("collection_path") or None,
        details=details,
    )
    refreshed_entries = build_quality_queue_entries(limit=1000)
    return refreshed_entries.get(entry.get("queue_key")) or entry


@app.route("/quality/duplicate-action", methods=["POST"])
def quality_duplicate_action():
    action = (request.form.get("action") or "").strip()
    if action not in QUALITY_DUPLICATE_ACTIONS:
        flash("Select a valid duplicate action.", "error")
        return redirect_back_or("global_contact_quality")

    duplicate_type = (request.form.get("duplicate_type") or "").strip()
    duplicate_value = (request.form.get("duplicate_value") or "").strip()
    confidence = (request.form.get("confidence") or "").strip()
    score_raw = (request.form.get("match_score") or "").strip()
    scope = (request.form.get("scope") or "global").strip()
    duplicate_contacts_raw = (request.form.get("duplicate_contacts") or "").strip()
    duplicate_contacts = []
    if duplicate_contacts_raw:
        try:
            payload = json.loads(duplicate_contacts_raw)
            if isinstance(payload, list):
                for item in payload[:4]:
                    if isinstance(item, dict):
                        duplicate_contacts.append(
                            {
                                "display": item.get("display") or "",
                                "email": item.get("email") or "",
                                "phone": item.get("phone") or "",
                                "profile_name": item.get("profile_name") or "",
                                "book_name": item.get("book_name") or "",
                                "profile_id": item.get("profile_id"),
                                "book_path": item.get("book_path") or "",
                                "filename": item.get("filename") or "",
                            }
                        )
        except Exception:
            duplicate_contacts = []
    profile_id_raw = (request.form.get("profile_id") or "").strip()
    collection_path = (request.form.get("collection_path") or "").strip()
    profile_id = None
    if profile_id_raw:
        try:
            profile_id = int(profile_id_raw)
        except Exception:
            profile_id = None
    queue_key = quality_queue_key_from_values(
        scope,
        profile_id,
        collection_path,
        duplicate_type,
        duplicate_value,
        action,
    )

    log_event(
        "quality_action",
        profile_id=profile_id,
        collection_path=collection_path or None,
        details={
            "action": action,
            "label": QUALITY_DUPLICATE_ACTIONS[action],
            "scope": scope,
            "duplicate_type": duplicate_type,
            "duplicate_value": duplicate_value,
            "match_score": score_raw,
            "confidence": confidence,
            "status": "queued",
            "queue_key": queue_key,
            "duplicate_contacts": duplicate_contacts,
        },
    )
    flash(
        f"{QUALITY_DUPLICATE_ACTIONS[action]} queued for {duplicate_type or 'duplicate'} group (non-destructive).",
        "success",
    )
    if profile_id and collection_path:
        return redirect_back_or("profile_contact_quality", profile_id=profile_id, collection_path=collection_path)
    return redirect_back_or("global_contact_quality")


@app.route("/quality/review-queue")
def quality_review_queue():
    selected_scope = (request.args.get("scope") or "").strip()
    selected_status = (request.args.get("status") or "").strip()
    selected_action = (request.args.get("action") or "").strip()
    selected_profile_id = (request.args.get("profile_id") or "").strip()

    queue_entries = build_quality_queue_entries(limit=1000)
    profiles = credential_store.get_profiles()

    filtered_entries = []
    for entry in queue_entries.values():
        if selected_scope and entry["scope"] != selected_scope:
            continue
        if selected_status and entry["status"] != selected_status:
            continue
        if selected_action and entry["action"] != selected_action:
            continue
        if selected_profile_id and str(entry.get("profile_id") or "") != selected_profile_id:
            continue
        if entry.get("action") == "recommend_merge" and not entry.get("can_preview_merge"):
            entry["merge_preview_unavailable_reason"] = (
                "Source contact references are missing, so merge preview cannot be opened for this item."
            )
        filtered_entries.append(entry)

    filtered_entries.sort(
        key=lambda item: parse_iso_timestamp(item.get("status_updated_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    active_statuses = {"queued", "in_review"}
    active_entries = [entry for entry in filtered_entries if entry.get("status") in active_statuses]
    closed_entries = [entry for entry in filtered_entries if entry.get("status") not in active_statuses]
    return render_template(
        "quality_review_queue.html",
        title="Quality Review Queue",
        entries=filtered_entries,
        active_entries=active_entries,
        closed_entries=closed_entries,
        profiles=profiles,
        status_options=QUALITY_ACTION_STATUSES,
        status_descriptions=QUALITY_STATUS_DESCRIPTIONS,
        action_options=QUALITY_DUPLICATE_ACTIONS,
        filters={
            "scope": selected_scope,
            "status": selected_status,
            "action": selected_action,
            "profile_id": selected_profile_id,
        },
    )


@app.route("/quality/review-queue/merge-preview")
def quality_review_queue_merge_preview():
    queue_key = (request.args.get("queue_key") or "").strip()
    if not queue_key:
        flash("Missing queue item key.", "error")
        return redirect(url_for("quality_review_queue"))
    queue_entries = build_quality_queue_entries(limit=1000)
    entry = queue_entries.get(queue_key)
    if not entry:
        flash("Queue item not found.", "error")
        return redirect(url_for("quality_review_queue"))
    if entry.get("action") != "recommend_merge":
        flash("Merge preview is only available for merge recommendations.", "error")
        return redirect(url_for("quality_review_queue"))
    if entry.get("status") in {"resolved", "dismissed"}:
        flash("This queue item is closed and cannot be merged again from preview.", "error")
        return redirect(url_for("quality_review_queue", status=entry.get("status")))
    entry = hydrate_queue_entry_contact_refs(entry)

    contacts = entry.get("duplicate_contacts") or []
    actionable_contacts = [
        item for item in contacts
        if item.get("profile_id") and item.get("book_path") and item.get("filename")
    ]
    if len(actionable_contacts) < 2:
        flash("This queue item does not have enough contact references to merge safely.", "error")
        return redirect(url_for("quality_review_queue"))

    source_details = []
    parse_errors = []
    for item in actionable_contacts[:2]:
        try:
            profile_id = int(item.get("profile_id"))
            client = get_client_for_profile(profile_id)
            vcard_text, _etag = client.get_contact(f"{item['book_path'].rstrip('/')}/{item['filename']}")
            fields = vcard_to_dict(vcard_text)
            parsed = parse_vcard_contact(vcard_text)
            source_details.append(
                {
                    "ref": item,
                    "fields": fields,
                    "parsed": parsed,
                    "raw_vcard": vcard_text,
                }
            )
        except Exception as exc:
            parse_errors.append(str(exc))
    if len(source_details) < 2:
        flash(f"Unable to load source contacts for preview: {'; '.join(parse_errors) or 'unknown error'}", "error")
        return redirect(url_for("quality_review_queue"))

    merged_defaults = {
        "full_name": source_details[0]["fields"].get("full_name") or source_details[1]["fields"].get("full_name") or "",
        "organization": source_details[0]["fields"].get("organization") or source_details[1]["fields"].get("organization") or "",
        "job_title": source_details[0]["fields"].get("job_title") or source_details[1]["fields"].get("job_title") or "",
        "birthday": source_details[0]["fields"].get("birthday") or source_details[1]["fields"].get("birthday") or "",
        "address": source_details[0]["fields"].get("address") or source_details[1]["fields"].get("address") or "",
        "note": source_details[0]["fields"].get("note") or source_details[1]["fields"].get("note") or "",
        "emails": sorted(
            {
                email.strip()
                for source in source_details
                for email in (source["fields"].get("emails") or [])
                if email and email.strip()
            }
        ),
        "phones": sorted(
            {
                phone.strip()
                for source in source_details
                for phone in (source["fields"].get("phones") or [])
                if phone and phone.strip()
            }
        ),
    }
    default_destination = f"{source_details[0]['ref']['profile_id']}::{source_details[0]['ref']['book_path']}"
    destinations = build_contact_destinations()
    return render_template(
        "quality_merge_preview.html",
        title="Merge Preview",
        entry=entry,
        source_details=source_details,
        destinations=destinations,
        merged_defaults=merged_defaults,
        default_destination=default_destination,
    )


@app.route("/quality/review-queue/merge-apply", methods=["POST"])
def quality_review_queue_merge_apply():
    queue_key = (request.form.get("queue_key") or "").strip()
    destination = (request.form.get("destination") or "").strip()
    if not queue_key:
        flash("Missing queue key.", "error")
        return redirect(url_for("quality_review_queue"))
    if not destination:
        flash("Choose a destination address book for the merged contact.", "error")
        return redirect(url_for("quality_review_queue_merge_preview", queue_key=queue_key))
    if request.form.get("confirm_merge") != "yes":
        flash("Confirm the merge before applying mitigation.", "error")
        return redirect(url_for("quality_review_queue_merge_preview", queue_key=queue_key))

    queue_entries = build_quality_queue_entries(limit=1000)
    entry = queue_entries.get(queue_key)
    if not entry or entry.get("action") != "recommend_merge":
        flash("Merge queue item not found.", "error")
        return redirect(url_for("quality_review_queue"))
    if entry.get("status") in {"resolved", "dismissed"}:
        flash("This queue item is closed and cannot be mitigated again.", "error")
        return redirect(url_for("quality_review_queue", status=entry.get("status")))

    source_refs = [
        item for item in (entry.get("duplicate_contacts") or [])
        if item.get("profile_id") and item.get("book_path") and item.get("filename")
    ][:2]
    if len(source_refs) < 2:
        flash("Not enough source contacts to execute merge.", "error")
        return redirect(url_for("quality_review_queue"))

    try:
        dest_profile_raw, dest_path = destination.split("::", 1)
        dest_profile_id = int(dest_profile_raw)
        dest_path = dest_path.strip("/")
    except Exception:
        flash("Invalid destination selection.", "error")
        return redirect(url_for("quality_review_queue_merge_preview", queue_key=queue_key))

    merged_fields = {
        "full_name": (request.form.get("full_name") or "").strip(),
        "first_name": "",
        "last_name": "",
        "nickname": "",
        "organization": (request.form.get("organization") or "").strip(),
        "job_title": (request.form.get("job_title") or "").strip(),
        "birthday": (request.form.get("birthday") or "").strip(),
        "address": (request.form.get("address") or "").strip(),
        "note": (request.form.get("note") or "").strip(),
        "emails": parse_multivalue_form("emails"),
        "phones": parse_multivalue_form("phones"),
        "urls": [],
        "categories": [],
        "uid": str(uuid.uuid4()),
    }
    if not merged_fields["full_name"]:
        flash("Merged contact needs a full name.", "error")
        return redirect(url_for("quality_review_queue_merge_preview", queue_key=queue_key))

    source_vcards = []
    try:
        for source_ref in source_refs:
            source_client = get_client_for_profile(int(source_ref["profile_id"]))
            source_vcard, _etag = source_client.get_contact(
                f"{source_ref['book_path'].rstrip('/')}/{source_ref['filename']}"
            )
            source_vcards.append(source_vcard)
        merged_vcard = build_vcard_from_fields(merged_fields)
        for source_vcard in source_vcards:
            merged_vcard = merge_unknown_fields_into_vcard(source_vcard, merged_vcard)

        dest_client = get_client_for_profile(dest_profile_id)
        merged_filename = f"{merged_fields['uid']}.vcf"
        dest_client.put_contact(dest_path, merged_filename, merged_vcard)
    except Exception as exc:
        flash(f"Merge mitigation failed: {exc}", "error")
        return redirect(url_for("quality_review_queue_merge_preview", queue_key=queue_key))

    deleted_sources = []
    delete_failures = []
    touched_source_books = set()
    source_clients = {}
    for ref in source_refs:
        try:
            ref_profile_id = int(ref.get("profile_id"))
            ref_path = (ref.get("book_path") or "").strip("/")
            ref_filename = (ref.get("filename") or "").strip()
            if not ref_profile_id or not ref_path or not ref_filename:
                raise ValueError("invalid source reference")
            source_clients.setdefault(ref_profile_id, get_client_for_profile(ref_profile_id))
            source_clients[ref_profile_id].delete_contact(f"{ref_path}/{ref_filename}")
            touched_source_books.add((ref_profile_id, ref_path))
            deleted_sources.append({"profile_id": ref_profile_id, "book_path": ref_path, "filename": ref_filename})
            log_event(
                "delete",
                profile_id=ref_profile_id,
                collection_path=ref_path,
                contact_filename=ref_filename,
                details={"reason": "merge_mitigation"},
            )
        except Exception as exc:
            delete_failures.append(
                {
                    "profile_id": ref.get("profile_id"),
                    "book_path": ref.get("book_path"),
                    "filename": ref.get("filename"),
                    "error": str(exc),
                }
            )

    update_cached_contact_count(dest_profile_id, dest_path, None)
    for touched_profile_id, touched_path in touched_source_books:
        update_cached_contact_count(touched_profile_id, touched_path, None)

    merged_status = "resolved" if not delete_failures else "in_review"
    merged_status_label = QUALITY_ACTION_STATUSES[merged_status]
    mitigation_mode = "merge_applied_and_sources_deleted" if not delete_failures else "merge_created_sources_not_fully_deleted"
    log_event(
        "quality_action_status",
        details={
            "queue_key": queue_key,
            "status": merged_status,
            "status_label": merged_status_label,
            "mitigation": mitigation_mode,
            "dest_profile_id": dest_profile_id,
            "dest_path": dest_path,
            "dest_filename": merged_filename,
            "source_deleted_count": len(deleted_sources),
            "source_total_count": len(source_refs),
            "source_delete_failures": delete_failures,
        },
    )
    if delete_failures:
        flash(
            "Merged contact created, but one or more source duplicates could not be removed. Item moved to In Review for follow-up.",
            "error",
        )
        return redirect(url_for("quality_review_queue", status="in_review"))
    flash(f"Mitigation applied. Merged contact created in {dest_path} and source duplicates were removed.", "success")
    return redirect(url_for("quality_review_queue", status="resolved"))


@app.route("/quality/review-queue/status", methods=["POST"])
def quality_review_queue_status():
    queue_key = (request.form.get("queue_key") or "").strip()
    status = (request.form.get("status") or "").strip()
    current_status = (request.form.get("current_status") or "").strip()
    if not queue_key:
        flash("Unable to update queue item: missing key.", "error")
        return redirect_back_or("quality_review_queue")
    if status not in QUALITY_ACTION_STATUSES:
        flash("Choose a valid queue status.", "error")
        return redirect_back_or("quality_review_queue")
    if current_status and status == current_status:
        flash("That item is already in this stage.", "error")
        return redirect_back_or("quality_review_queue")

    details = {
        "queue_key": queue_key,
        "status": status,
        "status_label": QUALITY_ACTION_STATUSES[status],
    }
    log_event("quality_action_status", details=details)
    flash(f"Queue item moved to {QUALITY_ACTION_STATUSES[status]}.", "success")
    return redirect_back_or("quality_review_queue")


def empty_contact_fields():
    return {
        "uid": "",
        "full_name": "",
        "first_name": "",
        "last_name": "",
        "nickname": "",
        "organization": "",
        "job_title": "",
        "birthday": "",
        "address": "",
        "note": "",
        "emails": [],
        "phones": [],
        "urls": [],
        "categories": [],
    }


def parse_multivalue_form(field_name):
    values = []
    for raw in request.form.getlist(field_name):
        text = (raw or "").strip()
        if not text:
            continue
        # Accept either repeated values or newline/comma-separated blocks.
        parts = re.split(r"[\r\n,]+", text)
        for part in parts:
            cleaned = (part or "").strip()
            if cleaned:
                values.append(cleaned)
    if values:
        # Preserve order while removing duplicates.
        return list(dict.fromkeys(values))
    fallback = request.form.get(field_name, "")
    parts = re.split(r"[\r\n,]+", fallback or "")
    return [part.strip() for part in parts if part.strip()]


def contact_fields_from_form(existing_uid=None):
    return {
        "uid": existing_uid,
        "full_name": request.form.get("full_name", "").strip(),
        "first_name": request.form.get("first_name", "").strip(),
        "last_name": request.form.get("last_name", "").strip(),
        "nickname": request.form.get("nickname", "").strip(),
        "organization": request.form.get("organization", "").strip(),
        "job_title": request.form.get("job_title", "").strip(),
        "birthday": request.form.get("birthday", "").strip(),
        "address": request.form.get("address", "").strip(),
        "note": request.form.get("note", "").strip(),
        "emails": parse_multivalue_form("emails"),
        "phones": parse_multivalue_form("phones"),
        "urls": parse_multivalue_form("urls"),
        "categories": parse_multivalue_form("categories"),
    }


def parse_global_contact_selection(value):
    parts = (value or "").split("::", 2)
    if len(parts) != 3:
        raise ValueError("Invalid contact selection")
    profile_id = int(parts[0])
    collection_path = parts[1].strip("/")
    filename = parts[2].strip()
    if not collection_path or not filename:
        raise ValueError("Invalid contact selection")
    return profile_id, collection_path, filename


def parse_iso_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def log_event(action, profile_id=None, collection_path=None, contact_filename=None, details=None):
    try:
        credential_store.add_event(
            action=action,
            profile_id=profile_id,
            collection_path=collection_path,
            contact_filename=contact_filename,
            details=details or {},
            source="app",
        )
    except Exception:
        app.logger.debug("Failed to log operation event %s", action, exc_info=True)


def security_status():
    secret_present = bool(app.config.get("PROFILE_SECRET"))
    return {
        "fernet_available": bool(HAS_FERNET),
        "secret_configured": secret_present,
        "encryption_enabled": bool(HAS_FERNET and secret_present),
        "guidance": (
            "Set PROFILE_SECRET_KEY to enable encrypted stored passwords."
            if not secret_present
            else ""
        ),
    }


def redirect_back_or(endpoint, **values):
    """Redirect to a local `next` path when present, else to a safe endpoint."""
    next_path = (request.form.get("next") or request.args.get("next") or "").strip()
    if next_path.startswith("/") and not next_path.startswith("//"):
        return redirect(next_path)
    return redirect(url_for(endpoint, **values))


def make_client():
    """Build a RadicaleClient from the active session connection."""
    active = get_active_connection()
    if not active:
        app.logger.debug("No active connection available to create client")
        return None
    app.logger.debug("Creating RadicaleClient for server %s", active["server_url"])
    return RadicaleClient(active["server_url"], active["username"], active["password"])


@app.route("/", methods=["GET", "POST"])
def login():
    """Render the login page and handle profile selection and authentication."""
    profiles = credential_store.get_profiles()

    app.logger.debug("Login page accessed via %s", request.method)
    form = {
        "server_url": app.config["DEFAULT_RADICALE_URL"],
        "username": "",
        "password": "",
        "profile_name": "",
        "save_profile": False,
    }
    selected_profile_id = ""

    if request.method == "POST":
        if request.form.get("profile_id"):
            selected_profile_id = request.form["profile_id"]
            app.logger.debug("Selected profile id %s", selected_profile_id)
            profile = credential_store.get_profile(selected_profile_id)
            if profile:
                form["server_url"] = profile["server_url"]
                form["username"] = profile["username"]
                form["password"] = profile.get("password") or ""
                form["profile_name"] = profile["name"]
                app.logger.debug("Loaded profile %s for login", profile["name"])
            else:
                app.logger.warning("Selected profile id %s could not be loaded", selected_profile_id)
        form["password"] = request.form.get("password", "")
        form["profile_name"] = request.form.get("profile_name", "").strip()
        form["save_profile"] = bool(request.form.get("save_profile"))
        form["username"] = request.form.get("username", "").strip()
        app.logger.debug(
            "Login form submitted server=%s username=%s save_profile=%s direct_login=%s",
            form["server_url"],
            form["username"],
            form["save_profile"],
            not selected_profile_id,
        )

        if not form["server_url"] or not form["username"] or not form["password"]:
            app.logger.warning(
                "Login form validation failed: server=%s username=%s password_present=%s",
                bool(form["server_url"]),
                bool(form["username"]),
                bool(form["password"]),
            )
            flash("Server URL, username, and password are required.", "error")
            return render_template(
                "login.html",
                title="Login",
                profiles=profiles,
                form=form,
                selected_profile_id=selected_profile_id,
            )

        try:
            client = RadicaleClient(form["server_url"], form["username"], form["password"])
            books = client.discover_addressbooks()
            app.logger.debug("Login discovery found %d books", len(books))
            if not books:
                app.logger.warning("Login discovery succeeded but no books were found")
                app.logger.debug(
                    "Login discovery details: server=%s username=%s selected_profile=%s no_books=True",
                    form["server_url"],
                    form["username"],
                    bool(selected_profile_id),
                )
                if not form["save_profile"] and not selected_profile_id:
                    flash(
                        "Connected, but no address books were found. Save a connection profile first, then create an address book from Connections.",
                        "error",
                    )
                    return render_template(
                        "login.html",
                        title="Login",
                        profiles=profiles,
                        form=form,
                        selected_profile_id=selected_profile_id,
                    )
                flash(
                    "Connected, but no address books were found. Create one from the Connections page.",
                    "success",
                )

            set_active_connection(form["server_url"], form["username"], form["password"])
            app.logger.warning(
                "SAVE_PROFILE=%s PROFILE_NAME='%s' DIRECT_LOGIN=%s",
                request.form.get("save_profile"),
                form["profile_name"],
                not selected_profile_id,
            )
            if form["save_profile"]:
                profile_name = form["profile_name"] or f"{form['username']}@{form['server_url']}"
                app.logger.warning("ABOUT TO CREATE PROFILE")
                try:
                    profile_id = credential_store.create_profile(
                        profile_name,
                        form["server_url"],
                        form["username"],
                        form["password"],
                        enabled=True,
                    )
                    if books:
                        books_with_counts = fill_missing_contact_counts(profile_id, client, books)
                        credential_store.save_or_update_address_books(profile_id, books_with_counts)
                    app.logger.warning("PROFILE CREATED id=%s", profile_id)
                    app.logger.warning(
                        "PROFILES AFTER CREATE: %s",
                        credential_store.get_profiles(),
                    )
                except Exception:
                    app.logger.exception("Failed to create profile %s", profile_name)

            if not books:
                return redirect(url_for("connections"))
            return redirect(url_for("dashboard"))
        except Exception as exc:
            app.logger.exception("Login failed for server %s user %s", form["server_url"], form["username"])
            flash(f"Connection failed: {exc}", "error")
            return render_template("login.html", title="Login", profiles=profiles, form=form)

    return render_template(
        "login.html",
        title="Login",
        profiles=profiles,
        form=form,
        selected_profile_id=selected_profile_id,
    )


@app.route("/logout")
def logout():
    """Disconnect the current session and return to the login page."""
    app.logger.info("User logging out")
    clear_active_connection()
    flash("Disconnected from Radicale.", "success")
    return redirect(url_for("login"))


@app.route("/connections")
def connections():
    """List saved connection profiles and their cached address books."""
    app.logger.debug("Connections list requested")
    profiles = credential_store.get_profiles()
    for p in profiles:
        try:
            books = credential_store.get_cached_address_books(p["id"]) or []
            normalized_books = [normalize_book_for_template(book) for book in books]
            p["books"] = normalized_books
            p["book_count"] = len(normalized_books)
            p["contact_count"] = sum((book.get("contact_count") or 0) for book in normalized_books)
            if p.get("last_error"):
                p["health_icon"] = "🔴"
                p["health_label"] = "Unhealthy"
            elif p.get("last_successful_connect_at"):
                p["health_icon"] = "🟢"
                p["health_label"] = "Healthy"
            else:
                p["health_icon"] = "🟡"
                p["health_label"] = "Not tested yet"
        except Exception:
            p["books"] = []
            p["book_count"] = 0
            p["contact_count"] = 0
            p["health_icon"] = "🟡"
            p["health_label"] = "Unknown"
    return render_template(
        "connections.html",
        title="Connections",
        profiles=profiles,
    )


@app.route("/connections/<int:profile_id>")
def connection_detail(profile_id):
    profile = credential_store.get_profile(profile_id)
    if not profile:
        flash("Profile not found.", "error")
        return redirect(url_for("connections"))
    books = credential_store.get_cached_address_books(profile_id) or []
    profile["books"] = [normalize_book_for_template(book) for book in books]
    last_success = profile.get("last_successful_connect_at")
    has_error = bool(profile.get("last_error"))
    if has_error:
        health_state = "error"
        health_label = "Unhealthy"
        health_icon = "🔴"
    elif last_success:
        health_state = "healthy"
        health_label = "Healthy"
        health_icon = "🟢"
    else:
        health_state = "warn"
        health_label = "Not tested yet"
        health_icon = "🟡"
    return render_template(
        "connections_detail.html",
        title=f"Connection - {profile.get('name')}",
        profile=profile,
        health_state=health_state,
        health_label=health_label,
        health_icon=health_icon,
    )


@app.route("/connections/export")
def export_connections():
    export_data = {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "profiles": [],
    }
    for profile in credential_store.get_profiles():
        full = credential_store.get_profile(profile["id"]) or {}
        export_data["profiles"].append(
            {
                "name": profile.get("name"),
                "server_url": profile.get("server_url"),
                "username": profile.get("username"),
                "password": full.get("password"),
                "enabled": bool(profile.get("enabled")),
                "group_name": profile.get("group_name", ""),
                "tags": profile.get("tags") or [],
                "books": credential_store.get_cached_address_books(profile["id"]) or [],
            }
        )
    content = json.dumps(export_data, indent=2)
    log_event("profile_export", details={"profiles": len(export_data["profiles"])})
    return send_file(
        BytesIO(content.encode("utf-8")),
        download_name=f"radicale-connections-{datetime.now(timezone.utc).date()}.json",
        mimetype="application/json",
        as_attachment=True,
    )


@app.route("/connections/import", methods=["POST"])
def import_connections():
    upload = request.files.get("connections_file")
    if not upload:
        flash("Choose a connection export JSON file.", "error")
        return redirect(url_for("connections"))
    try:
        payload = json.loads(upload.read().decode("utf-8", errors="replace"))
        profiles = payload.get("profiles") if isinstance(payload, dict) else []
        if not isinstance(profiles, list):
            raise ValueError("Invalid profiles payload")
        imported = 0
        for item in profiles:
            if not isinstance(item, dict):
                continue
            profile_id = credential_store.create_profile(
                item.get("name") or f"{item.get('username', 'user')}@{item.get('server_url', 'server')}",
                item.get("server_url") or "",
                item.get("username") or "",
                password=item.get("password"),
                enabled=bool(item.get("enabled", True)),
                group_name=item.get("group_name", ""),
                tags=item.get("tags") or [],
            )
            books = item.get("books") or []
            if books:
                credential_store.save_or_update_address_books(profile_id, books)
            imported += 1
        log_event("profile_import", details={"profiles": imported})
        flash(f"Imported {imported} connection profiles.", "success")
    except Exception as exc:
        app.logger.exception("Connection import failed")
        flash(f"Connection import failed: {exc}", "error")
    return redirect_back_or("connections")


@app.route("/connections/new", methods=["GET", "POST"])
def new_connection():
    app.logger.debug("New connection page %s", request.method)
    form = {
        "server_url": app.config["DEFAULT_RADICALE_URL"],
        "username": "",
        "password": "",
        "profile_name": "",
        "group_name": "",
        "tags": "",
        "enabled": True,
    }
    if request.method == "POST":
        name = (
            request.form.get("profile_name", "").strip()
            or request.form.get("name", "").strip()
        )
        server_url = request.form.get("server_url", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        group_name = request.form.get("group_name", "").strip()
        tags = request.form.get("tags", "").strip()
        enabled = bool(request.form.get("enabled"))
        form.update(
            {
                "server_url": server_url,
                "username": username,
                "password": password,
                "profile_name": name,
                "group_name": group_name,
                "tags": tags,
                "enabled": enabled,
            }
        )
        if not name or not server_url or not username:
            flash("Name, server URL and username are required.", "error")
            return render_template("connections_new.html", form=form)
        try:
            credential_store.create_profile(
                name,
                server_url,
                username,
                password=password or None,
                enabled=enabled,
                group_name=group_name,
                tags=tags,
            )
            flash("Connection saved.", "success")
            return redirect(url_for("connections"))
        except Exception as exc:
            app.logger.exception("Failed to create profile")
            flash(f"Failed to save connection: {exc}", "error")
            return render_template("connections_new.html", form=form)
    return render_template("connections_new.html", form=form)


@app.route("/connections/<int:profile_id>/edit", methods=["GET", "POST"])
def edit_connection(profile_id):
    app.logger.debug("Edit connection %s %s", profile_id, request.method)
    profile = credential_store.get_profile(profile_id)
    if not profile:
        flash("Profile not found.", "error")
        return redirect_back_or("connections")

    form = {
        "server_url": profile.get("server_url") or "",
        "username": profile.get("username") or "",
        "password": "",
        "profile_name": profile.get("name") or "",
        "group_name": profile.get("group_name") or "",
        "tags": profile.get("tags_text") or ", ".join(profile.get("tags") or []),
        "enabled": profile.get("enabled", True),
    }

    if request.method == "POST":
        name = (
            request.form.get("profile_name", "").strip()
            or request.form.get("name", "").strip()
        )
        server_url = request.form.get("server_url", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        group_name = request.form.get("group_name", "").strip()
        tags = request.form.get("tags", "").strip()
        enabled = bool(request.form.get("enabled"))
        form.update(
            {
                "server_url": server_url,
                "username": username,
                "password": "",
                "profile_name": name,
                "group_name": group_name,
                "tags": tags,
                "enabled": enabled,
            }
        )

        if not name or not server_url or not username:
            flash("Name, server URL and username are required.", "error")
            return render_template("connections_edit.html", form=form, profile_id=profile_id)

        try:
            credential_store.update_profile(
                profile_id,
                name=name,
                server_url=server_url,
                username=username,
                password=password if password else None,
                group_name=group_name,
                tags=tags,
                enabled=enabled,
            )
            flash("Connection updated.", "success")
            return redirect(url_for("connections"))
        except Exception as exc:
            app.logger.exception("Failed to update profile %s", profile_id)
            flash(f"Failed to update connection: {exc}", "error")
            return render_template("connections_edit.html", form=form, profile_id=profile_id)

    return render_template("connections_edit.html", form=form, profile_id=profile_id)


@app.route("/books")
def address_books():
    """Show a compact list of cached address books across enabled profiles."""
    profiles = credential_store.get_enabled_profiles()
    books = []
    for profile in profiles:
        cached = credential_store.get_cached_address_books(profile["id"]) or []
        for book in cached:
            normalized = normalize_book_for_template(book)
            books.append(
                {
                    "profile_id": profile["id"],
                    "profile_name": profile["name"],
                    "display_name": normalized["display_name"],
                    "path": normalized["path"],
                    "contact_count": normalized.get("contact_count"),
                    "last_seen_at": normalized.get("last_seen_at"),
                }
            )
    books.sort(key=lambda item: (item["profile_name"].casefold(), item["display_name"].casefold()))
    return render_template("address_books.html", title="Address Books", books=books)


@app.route("/system")
def system_menu():
    """Show advanced and debug tools that are hidden from primary mobile navigation."""
    system_links = [
        {
            "name": "Connections",
            "description": "Manage connection profiles, test connectivity, and refresh books.",
            "endpoint": "connections",
        },
        {
            "name": "Credential Security",
            "description": "Encryption readiness and profile-secret status.",
            "endpoint": "security_settings",
        },
        {
            "name": "Event Log",
            "description": "Track recent imports, edits, deletes, moves, and exports.",
            "endpoint": "system_events",
        },
        {
            "name": "Quality Review Queue",
            "description": "Review queued duplicate actions and move items through status states.",
            "endpoint": "quality_review_queue",
        },
        {
            "name": "Routes Explorer",
            "description": "Browse all registered Flask endpoints and methods.",
            "endpoint": "system_routes",
        },
        {
            "name": "Health JSON",
            "description": "Raw app health payload with uptime and dependency status.",
            "endpoint": "health",
        },
        {
            "name": "Readiness JSON",
            "description": "Readiness probe payload used by deployments.",
            "endpoint": "ready",
        },
        {
            "name": "Debug Profiles",
            "description": "Diagnostic profile export for local troubleshooting.",
            "endpoint": "debug_profiles",
        },
    ]
    return render_template("system_menu.html", title="Settings", system_links=system_links)


@app.route("/system/events")
def system_events():
    """Show recent operation events for audit and troubleshooting."""
    selected_action = request.args.get("action", "").strip()
    limit_raw = request.args.get("limit", "").strip()
    try:
        limit = int(limit_raw) if limit_raw else 100
    except Exception:
        limit = 100
    limit = max(10, min(limit, 250))

    available_actions = sorted(EVENT_ACTION_LABELS.keys())
    action_filters = [selected_action] if selected_action in available_actions else None
    events = credential_store.get_recent_events(limit=limit, actions=action_filters)

    profiles = credential_store.get_profiles()
    profile_names = {profile["id"]: profile["name"] for profile in profiles}
    for event in events:
        action = event.get("action") or ""
        event["action_label"] = EVENT_ACTION_LABELS.get(action, action.replace("_", " ").title() or "Unknown action")
        event["profile_name"] = profile_names.get(event.get("profile_id")) or "System"
        details = event.get("details") or {}
        if details:
            event["details_summary"] = ", ".join(f"{key}: {value}" for key, value in details.items())
        else:
            event["details_summary"] = ""

    return render_template(
        "system_events.html",
        title="Event Log",
        events=events,
        action_options=available_actions,
        selected_action=selected_action if selected_action in available_actions else "",
        selected_limit=limit,
        event_action_labels=EVENT_ACTION_LABELS,
    )


@app.route("/system/security")
def security_settings():
    return render_template(
        "security.html",
        title="Credential Security",
        security=security_status(),
    )


@app.route("/manifest.json")
def pwa_manifest():
    with open(os.path.join(app.static_folder, "manifest.json"), "r", encoding="utf-8") as manifest_file:
        manifest = manifest_file.read()
    response = Response(manifest, mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/service-worker.js")
def pwa_service_worker():
    with open(os.path.join(app.static_folder, "service-worker.js"), "r", encoding="utf-8") as sw_file:
        script = sw_file.read()
    response = Response(script, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/system/routes")
def system_routes():
    """Show all registered Flask routes and metadata for discovery."""
    routes = []
    for rule in app.url_map.iter_rules():
        # skip static endpoints
        if rule.endpoint.startswith("static"):
            continue
        methods = sorted([m for m in rule.methods if m not in ("HEAD", "OPTIONS")])
        view_fn = app.view_functions.get(rule.endpoint)
        desc = (view_fn.__doc__ or "").strip() if view_fn else ""
        routes.append({"endpoint": rule.endpoint, "path": str(rule), "methods": methods, "description": desc})
    routes = sorted(routes, key=lambda r: r["path"])
    return render_template("system_routes.html", routes=routes)


@app.route("/connections/<int:profile_id>/delete", methods=["POST"])
def delete_connection(profile_id):
    app.logger.debug("Delete connection %s requested", profile_id)
    try:
        credential_store.delete_profile(profile_id)
        flash("Connection deleted.", "success")
    except Exception as exc:
        app.logger.exception("Failed to delete profile %s", profile_id)
        flash(f"Failed to delete connection: {exc}", "error")
    return redirect(url_for("connections"))


@app.route("/connections/<int:profile_id>/test", methods=["POST"])
def test_connection(profile_id):
    app.logger.debug("Test connection %s", profile_id)
    profile = credential_store.get_profile(profile_id)
    if not profile:
        flash("Profile not found.", "error")
        return redirect(url_for("connections"))
    # Allow providing an override password for testing without saving
    password = request.form.get("password") or profile.get("password")
    try:
        client = RadicaleClient(profile["server_url"], profile["username"], password)
        books = client.discover_addressbooks()
        books = fill_missing_contact_counts(profile_id, client, books)
        credential_store.save_or_update_address_books(profile_id, books)
        credential_store.update_connection_status(profile_id, True)
        log_event("connection_test", profile_id=profile_id, details={"books": len(books), "result": "success"})
        flash(f"Connection test succeeded: found {len(books)} books.", "success")
    except Exception as exc:
        credential_store.update_connection_status(profile_id, False, str(exc))
        log_event("connection_test", profile_id=profile_id, details={"result": "failed", "error": str(exc)})
        app.logger.exception("Connection test failed for %s", profile_id)
        flash(f"Connection test failed: {exc}", "error")
    return redirect(url_for("connections"))


@app.route("/connections/<int:profile_id>/refresh", methods=["POST"])
def refresh_addressbooks(profile_id):
    app.logger.debug("Refresh address books for %s", profile_id)
    profile = credential_store.get_profile(profile_id)
    if not profile:
        flash("Profile not found.", "error")
        return redirect(url_for("connections"))
    if not profile.get("password"):
        flash("Cannot refresh address books: profile has no stored password.", "error")
        return redirect(url_for("connections"))
    try:
        client = RadicaleClient(profile["server_url"], profile["username"], profile["password"])
        books = client.discover_addressbooks()
        books = fill_missing_contact_counts(profile_id, client, books)
        credential_store.save_or_update_address_books(profile_id, books)
        credential_store.update_connection_status(profile_id, True)
        log_event("addressbook_refresh", profile_id=profile_id, details={"books": len(books), "result": "success"})
        flash(f"Refreshed {len(books)} address books.", "success")
    except Exception as exc:
        credential_store.update_connection_status(profile_id, False, str(exc))
        log_event("addressbook_refresh", profile_id=profile_id, details={"result": "failed", "error": str(exc)})
        app.logger.exception("Refresh failed for %s", profile_id)
        flash(f"Refresh failed: {exc}", "error")
    return redirect_back_or("connections")


@app.route("/connections/<int:profile_id>/addressbooks/create", methods=["POST"])
def create_addressbook(profile_id):
    profile = credential_store.get_profile(profile_id)
    if not profile:
        flash("Profile not found.", "error")
        return redirect_back_or("connections")
    if not profile.get("password"):
        flash("Cannot create address book: profile has no stored password.", "error")
        return redirect_back_or("connections")

    display_name = request.form.get("display_name", "").strip()
    if not display_name:
        flash("Address book name is required.", "error")
        return redirect_back_or("connections")

    try:
        existing_paths = {
            book["path"]
            for book in (credential_store.get_cached_address_books(profile_id) or [])
        }
        base_slug = slugify_addressbook_name(display_name)
        path = f"{profile['username'].strip('/')}/{base_slug}"
        suffix = 2
        while path in existing_paths:
            path = f"{profile['username'].strip('/')}/{base_slug}-{suffix}"
            suffix += 1

        client = RadicaleClient(profile["server_url"], profile["username"], profile["password"])
        client.create_addressbook(path, display_name)
        books = client.discover_addressbooks()
        credential_store.save_or_update_address_books(profile_id, books)
        flash("Address book created.", "success")
    except Exception as exc:
        app.logger.exception("Failed to create address book for profile %s", profile_id)
        flash(f"Failed to create address book: {exc}", "error")
    return redirect_back_or("connections")


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/rename", methods=["POST"])
def rename_addressbook(profile_id, collection_path):
    profile = credential_store.get_profile(profile_id)
    if not profile:
        flash("Profile not found.", "error")
        return redirect_back_or("connections")
    if not profile.get("password"):
        flash("Cannot rename address book: profile has no stored password.", "error")
        return redirect_back_or("connections")

    display_name = request.form.get("display_name", "").strip()
    if not display_name:
        flash("Address book name is required.", "error")
        return redirect_back_or("connections")

    try:
        client = RadicaleClient(profile["server_url"], profile["username"], profile["password"])
        client.rename_addressbook(collection_path, display_name)
        books = client.discover_addressbooks()
        credential_store.save_or_update_address_books(profile_id, books)
        flash("Address book renamed.", "success")
    except Exception as exc:
        app.logger.exception("Failed to rename address book %s", collection_path)
        flash(f"Failed to rename address book: {exc}", "error")
    return redirect_back_or("connections")


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/delete", methods=["POST"])
def delete_addressbook(profile_id, collection_path):
    profile = credential_store.get_profile(profile_id)
    if not profile:
        flash("Profile not found.", "error")
        return redirect_back_or("connections")
    if not profile.get("password"):
        flash("Cannot delete address book: profile has no stored password.", "error")
        return redirect_back_or("connections")

    try:
        client = RadicaleClient(profile["server_url"], profile["username"], profile["password"])
        client.delete_addressbook(collection_path)
        books = client.discover_addressbooks()
        credential_store.delete_address_books(profile_id)
        credential_store.save_or_update_address_books(profile_id, books)
        flash("Address book deleted.", "success")
    except Exception as exc:
        app.logger.exception("Failed to delete address book %s", collection_path)
        flash(f"Failed to delete address book: {exc}", "error")
    return redirect(url_for("connections"))


@app.route("/dashboard")
def dashboard():
    """Show enabled connection profiles with their discovered/cached address books."""
    app.logger.debug("Dashboard requested")
    # Debug: log all stored profiles for troubleshooting visibility
    try:
        all_profiles = credential_store.get_profiles()
        app.logger.debug("Dashboard loaded %d total profiles", len(all_profiles))
        for p in all_profiles:
            try:
                app.logger.debug(
                    "Profile id=%s name=%s enabled=%s",
                    p.get("id"),
                    p.get("name"),
                    p.get("enabled"),
                )
            except Exception:
                app.logger.debug("Profile logging failed for entry: %s", p)
    except Exception:
        app.logger.debug("Failed to load profiles for dashboard debug logging")
    profiles = credential_store.get_enabled_profiles()
    display = []
    for p in profiles:
        entry = {**p}
        try:
            full = credential_store.get_profile(p["id"]) or {}
            p_password = full.get("password")
            books = credential_store.get_cached_address_books(p["id"]) or []
            # If no cache, try to discover now if password available
            if not books and p_password:
                try:
                    client = RadicaleClient(p["server_url"], p["username"], p_password)
                    discovered = client.discover_addressbooks()
                    credential_store.save_or_update_address_books(p["id"], discovered)
                    books = credential_store.get_cached_address_books(p["id"]) or []
                except Exception as exc:
                    app.logger.warning("Discovery for profile %s failed: %s", p.get("name"), exc)
            if books and p_password:
                client = RadicaleClient(p["server_url"], p["username"], p_password)
                books = fill_missing_contact_counts(p["id"], client, books)
            else:
                books = [normalize_book_for_template(book) for book in books]
            entry["books"] = books
        except Exception:
            entry["books"] = []
        display.append(entry)
    # compute summary metrics from cached data
    try:
        all_profiles = credential_store.get_profiles()
        total_connections = len(all_profiles)
        total_address_books = 0
        total_contacts = 0
        for prof in all_profiles:
            books = credential_store.get_cached_address_books(prof["id"]) or []
            total_address_books += len(books)
            for b in books:
                total_contacts += (b.get("contact_count") or 0)
    except Exception:
        total_connections = len(profiles)
        total_address_books = sum(len(p.get("books") or []) for p in profiles)
        total_contacts = sum((b.get("contact_count") or 0) for p in profiles for b in (p.get("books") or []))

    metrics = {
        "app_version": app.config.get("APP_VERSION"),
        "total_connections": total_connections,
        "total_address_books": total_address_books,
        "total_contacts": total_contacts,
        "duplicates": None,
        "issues": None,
    }
    since = datetime.now(timezone.utc) - timedelta(days=7)
    since_iso = since.isoformat().replace("+00:00", "Z")
    metrics["recent_imports"] = credential_store.count_recent_events("import", since_iso)
    metrics["recent_exports"] = (
        credential_store.count_recent_events("export", since_iso)
        + credential_store.count_recent_events("addressbook_export", since_iso)
        + credential_store.count_recent_events("contact_export", since_iso)
        + credential_store.count_recent_events("connection_export", since_iso)
        + credential_store.count_recent_events("backup_export", since_iso)
    )
    metrics["recent_moves"] = credential_store.count_recent_events("move", since_iso)
    metrics["recent_deletes"] = credential_store.count_recent_events("delete", since_iso)
    safe_profiles = locals().get("all_profiles") or credential_store.get_profiles()
    metrics["connection_health"] = {
        "healthy": sum(1 for profile in safe_profiles if not profile.get("last_error")),
        "unhealthy": sum(1 for profile in safe_profiles if profile.get("last_error")),
    }
    recent_backup = credential_store.get_recent_events(limit=1, actions=["backup_export"])
    last_backup_at = recent_backup[0]["created_at"] if recent_backup else None
    metrics["backup_health"] = {
        "last_backup_at": last_backup_at,
        "status": "healthy" if last_backup_at else "warning",
    }
    quality_events = credential_store.get_recent_events(limit=500, actions=["quality_scan"])
    latest_quality_by_book = {}
    for event in quality_events:
        key = (event.get("profile_id"), event.get("collection_path"))
        if key not in latest_quality_by_book:
            latest_quality_by_book[key] = event
    if latest_quality_by_book:
        metrics["duplicates"] = sum(
            int((event.get("details") or {}).get("duplicate_groups", 0))
            for event in latest_quality_by_book.values()
        )
        metrics["issues"] = sum(
            int((event.get("details") or {}).get("issues", 0))
            for event in latest_quality_by_book.values()
        )
        metrics["last_quality_scan_at"] = max(
            (
                parse_iso_timestamp(event.get("created_at"))
                for event in latest_quality_by_book.values()
            ),
            default=None,
        )
        if metrics["last_quality_scan_at"]:
            metrics["last_quality_scan_at"] = (
                metrics["last_quality_scan_at"]
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
    else:
        metrics["last_quality_scan_at"] = None
    recent_events = credential_store.get_recent_events(limit=8)

    return render_template(
        "dashboard.html",
        title="Dashboard",
        profiles=display,
        metrics=metrics,
        recent_events=recent_events,
    )


@app.route("/contacts")
def global_contacts():
    """Show all contacts across enabled profiles and address books."""
    search_query = request.args.get("q", "").strip()
    filter_profile_id = request.args.get("profile_id", "").strip()
    filter_book_path = request.args.get("book_path", "").strip()
    has_email = request.args.get("has_email") in ("1", "true", "on", "yes")
    has_phone = request.args.get("has_phone") in ("1", "true", "on", "yes")
    recent_days = request.args.get("recent_days", "").strip()
    recent_days_int = 0
    try:
        recent_days_int = int(recent_days) if recent_days else 0
    except Exception:
        recent_days_int = 0

    recent_keys = set()
    if recent_days_int > 0:
        since_iso = (datetime.now(timezone.utc) - timedelta(days=recent_days_int)).isoformat().replace("+00:00", "Z")
        recent_keys = set(
            credential_store.get_recent_contact_keys(
                since_iso,
                actions=["import", "create", "edit", "move", "copy", "duplicate"],
            )
        )

    contacts = []
    errors = []
    profiles = credential_store.get_enabled_profiles()
    for profile in profiles:
        full_profile = credential_store.get_profile(profile["id"]) or {}
        password = full_profile.get("password")
        books = credential_store.get_cached_address_books(profile["id"]) or []
        if not password:
            errors.append(f"{profile['name']}: no stored password")
            continue
        try:
            client = RadicaleClient(profile["server_url"], profile["username"], password)
        except Exception as exc:
            errors.append(f"{profile['name']}: {exc}")
            continue

        for book in books:
            normalized_book = normalize_book_for_template(book)
            try:
                listed = client.list_contacts(normalized_book["path"])
                update_cached_contact_count(profile["id"], normalized_book["path"], len(listed))
            except Exception as exc:
                errors.append(f"{profile['name']} / {normalized_book['display_name']}: {exc}")
                continue

            for item in listed:
                try:
                    parsed = vcard_to_dict(item["vcard"])
                    filename = get_contact_filename(item["href"])
                    profile_id = profile["id"]
                    normalized_path = normalized_book["path"].strip("/")
                    searchable = " ".join(
                        [
                            parsed.get("full_name") or "",
                            parsed.get("organization") or "",
                            " ".join(parsed.get("emails") or []),
                            " ".join(parsed.get("phones") or []),
                            normalized_book["display_name"],
                            profile["name"],
                            filename or "",
                        ]
                    ).casefold()
                    if search_query and search_query.casefold() not in searchable:
                        continue
                    if filter_profile_id and str(profile_id) != filter_profile_id:
                        continue
                    if filter_book_path and normalized_path != filter_book_path.strip("/"):
                        continue
                    if has_email and not parsed.get("emails"):
                        continue
                    if has_phone and not parsed.get("phones"):
                        continue
                    if recent_days_int > 0 and (profile_id, normalized_path, filename) not in recent_keys:
                        continue
                    parsed.update(
                        {
                            "filename": filename,
                            "profile_id": profile_id,
                            "profile_name": profile["name"],
                            "book_path": normalized_path,
                            "book_name": normalized_book["display_name"],
                        }
                    )
                    contacts.append(parsed)
                except Exception as exc:
                    errors.append(f"{profile['name']} / {normalized_book['display_name']}: skipped invalid contact ({exc})")

    contacts.sort(
        key=lambda contact: (
            (contact.get("full_name") or "").casefold(),
            (contact.get("profile_name") or "").casefold(),
            (contact.get("book_name") or "").casefold(),
            (contact.get("filename") or "").casefold(),
        )
    )
    destinations = build_contact_destinations()
    profile_books = []
    for profile in profiles:
        for book in credential_store.get_cached_address_books(profile["id"]) or []:
            normalized_book = normalize_book_for_template(book)
            profile_books.append(
                {
                    "profile_id": profile["id"],
                    "profile_name": profile["name"],
                    "path": normalized_book["path"].strip("/"),
                    "display_name": normalized_book["display_name"],
                }
            )
    return render_template(
        "global_contacts.html",
        title="All Contacts",
        contacts=contacts,
        destinations=destinations,
        errors=errors,
        profiles=profiles,
        profile_books=profile_books,
        search_filters={
            "q": search_query,
            "profile_id": filter_profile_id,
            "book_path": filter_book_path,
            "has_email": has_email,
            "has_phone": has_phone,
            "recent_days": recent_days_int,
        },
    )


@app.route("/contacts/quality")
def global_contact_quality():
    """Scan contact quality across all enabled connections with optional scope filters."""
    selected_profile_ids_raw = [value.strip() for value in request.args.getlist("profile_ids") if value.strip()]
    selected_book_keys = [value.strip() for value in request.args.getlist("book_keys") if value.strip()]
    selected_profile_ids = set()
    for value in selected_profile_ids_raw:
        try:
            selected_profile_ids.add(int(value))
        except Exception:
            continue
    selected_book_keys_set = set(selected_book_keys)

    contacts = []
    errors = []
    profiles = credential_store.get_enabled_profiles()
    profile_books = []
    scanned_books = []
    scanned_profile_ids = set()
    scoped_book_count = 0

    for profile in profiles:
        books = credential_store.get_cached_address_books(profile["id"]) or []
        for book in books:
            normalized_book = normalize_book_for_template(book)
            scoped_path = normalized_book["path"].strip("/")
            book_key = f"{profile['id']}::{scoped_path}"
            profile_books.append(
                {
                    "profile_id": profile["id"],
                    "profile_name": profile["name"],
                    "path": scoped_path,
                    "display_name": normalized_book["display_name"],
                    "key": book_key,
                }
            )

    for profile in profiles:
        if selected_profile_ids and profile["id"] not in selected_profile_ids:
            continue
        full_profile = credential_store.get_profile(profile["id"]) or {}
        password = full_profile.get("password")
        books = credential_store.get_cached_address_books(profile["id"]) or []
        if not password:
            errors.append(f"{profile['name']}: no stored password")
            continue
        try:
            client = RadicaleClient(profile["server_url"], profile["username"], password)
        except Exception as exc:
            errors.append(f"{profile['name']}: {exc}")
            continue

        for book in books:
            normalized_book = normalize_book_for_template(book)
            normalized_path = normalized_book["path"].strip("/")
            book_key = f"{profile['id']}::{normalized_path}"
            if selected_book_keys_set and book_key not in selected_book_keys_set:
                continue
            scoped_book_count += 1
            scanned_profile_ids.add(profile["id"])
            scanned_books.append({"profile_name": profile["name"], "book_name": normalized_book["display_name"], "book_path": normalized_path})
            try:
                listed = client.list_contacts(normalized_book["path"])
                update_cached_contact_count(profile["id"], normalized_book["path"], len(listed))
            except Exception as exc:
                errors.append(f"{profile['name']} / {normalized_book['display_name']}: {exc}")
                continue

            for item in listed:
                try:
                    parsed = vcard_to_dict(item["vcard"])
                    filename = get_contact_filename(item["href"])
                    parsed.update(
                        {
                            "filename": filename,
                            "profile_id": profile["id"],
                            "profile_name": profile["name"],
                            "book_path": normalized_path,
                            "book_name": normalized_book["display_name"],
                        }
                    )
                    contacts.append(parsed)
                except Exception as exc:
                    errors.append(f"{profile['name']} / {normalized_book['display_name']}: skipped invalid contact ({exc})")

    report = build_duplicate_report(contacts)
    report["duplicates"] = attach_queue_stage_to_duplicate_groups(
        report.get("duplicates") or [],
        scope="global",
    )
    log_event(
        "quality_scan",
        details={
            "scope": "global",
            "profiles_scanned": len(scanned_profile_ids),
            "books_scanned": scoped_book_count,
            "contacts_scanned": len(contacts),
            "duplicate_groups": len(report.get("duplicates") or []),
            "issues": len(report.get("issues") or []),
            "warnings": len(report.get("warnings") or []),
        },
    )
    return render_template(
        "global_contact_quality.html",
        title="Global Quality Scan",
        contacts=contacts,
        report=report,
        errors=errors,
        profiles=profiles,
        profile_books=profile_books,
        scanned_books=scanned_books,
        scope_filters={
            "profile_ids": [str(profile_id) for profile_id in sorted(selected_profile_ids)],
            "book_keys": list(selected_book_keys_set),
        },
        quality_actions=QUALITY_DUPLICATE_ACTIONS,
        quality_return_to=request.full_path.rstrip("?"),
    )


@app.route("/contacts/bulk", methods=["POST"])
def global_bulk_contacts():
    action = request.form.get("bulk_action")
    selected = [value for value in request.form.getlist("contact_ref") if value]
    if not selected:
        flash("Select at least one contact.", "error")
        return redirect(url_for("global_contacts"))

    refs = []
    try:
        refs = [parse_global_contact_selection(value) for value in selected]
    except Exception as exc:
        flash(f"Invalid contact selection: {exc}", "error")
        return redirect(url_for("global_contacts"))

    client_cache = {}

    def client_for(profile_id):
        if profile_id not in client_cache:
            client_cache[profile_id] = get_client_for_profile(profile_id)
        return client_cache[profile_id]

    if action == "export":
        exported = []
        failed = []
        for source_profile_id, source_path, filename in refs:
            try:
                vcard_text, _etag = client_for(source_profile_id).get_contact(f"{source_path}/{filename}")
                exported.append(vcard_text.strip())
            except Exception as exc:
                failed.append(f"{filename}: {exc}")
        if not exported:
            flash(f"Export failed: {'; '.join(failed) or 'no contacts could be exported'}", "error")
            return redirect(url_for("global_contacts"))
        if failed:
            flash(f"Exported {len(exported)} contacts. Failed: {'; '.join(failed)}", "error")
        log_event("export", details={"scope": "global", "count": len(exported), "failed": len(failed)})
        content = "\n".join(exported) + "\n"
        filename = f"radicale-global-selected-{datetime.now(timezone.utc).date()}.vcf"
        return send_file(
            BytesIO(content.encode("utf-8")),
            download_name=filename,
            mimetype="text/vcard",
            as_attachment=True,
        )

    if action == "delete":
        deleted = 0
        failed = []
        touched_books = set()
        for source_profile_id, source_path, filename in refs:
            try:
                client_for(source_profile_id).delete_contact(f"{source_path}/{filename}")
                touched_books.add((source_profile_id, source_path))
                deleted += 1
            except Exception as exc:
                failed.append(f"{filename}: {exc}")
        for touched_profile_id, touched_path in touched_books:
            update_cached_contact_count(touched_profile_id, touched_path, None)
        for source_profile_id, source_path, filename in refs:
            log_event("delete", profile_id=source_profile_id, collection_path=source_path, contact_filename=filename)
        if failed:
            flash(f"Deleted {deleted} contacts. Failed: {'; '.join(failed)}", "error")
        else:
            flash(f"Deleted {deleted} contacts.", "success")
        return redirect(url_for("global_contacts"))

    if action == "move":
        dest = request.form.get("dest")
        dest_profile_id = None
        dest_path = None
        if dest:
            try:
                parts = dest.split("::", 1)
                dest_profile_id = int(parts[0])
                dest_path = parts[1].strip("/")
            except Exception:
                dest_profile_id = None
                dest_path = None
        if not dest_profile_id or not dest_path:
            flash("Destination profile and path are required.", "error")
            return redirect(url_for("global_contacts"))

        moved = 0
        failed = []
        touched_books = {(dest_profile_id, dest_path)}
        try:
            dest_client = client_for(dest_profile_id)
        except Exception as exc:
            flash(f"Unable to access destination profile: {exc}", "error")
            return redirect(url_for("global_contacts"))

        for source_profile_id, source_path, filename in refs:
            if source_profile_id == dest_profile_id and source_path.strip("/") == dest_path:
                failed.append(f"{filename}: already in destination")
                continue
            try:
                source_client = client_for(source_profile_id)
                vcard_text, _etag = source_client.get_contact(f"{source_path}/{filename}")
                dest_client.put_contact(dest_path, filename, vcard_text)
                source_client.delete_contact(f"{source_path}/{filename}")
                touched_books.add((source_profile_id, source_path))
                moved += 1
                log_event(
                    "move",
                    profile_id=source_profile_id,
                    collection_path=source_path,
                    contact_filename=filename,
                    details={"dest_profile_id": dest_profile_id, "dest_path": dest_path},
                )
            except Exception as exc:
                failed.append(f"{filename}: {exc}")
        for touched_profile_id, touched_path in touched_books:
            update_cached_contact_count(touched_profile_id, touched_path, None)
        if failed:
            flash(f"Moved {moved} contacts. Failed: {'; '.join(failed)}", "error")
        else:
            flash(f"Moved {moved} contacts.", "success")
        return redirect(url_for("global_contacts"))

    if action == "copy":
        dest = request.form.get("dest")
        dest_profile_id = None
        dest_path = None
        if dest:
            try:
                parts = dest.split("::", 1)
                dest_profile_id = int(parts[0])
                dest_path = parts[1].strip("/")
            except Exception:
                dest_profile_id = None
                dest_path = None
        if not dest_profile_id or not dest_path:
            flash("Destination profile and path are required.", "error")
            return redirect(url_for("global_contacts"))
        copied = 0
        failed = []
        try:
            dest_client = client_for(dest_profile_id)
        except Exception as exc:
            flash(f"Unable to access destination profile: {exc}", "error")
            return redirect(url_for("global_contacts"))
        for source_profile_id, source_path, filename in refs:
            try:
                source_client = client_for(source_profile_id)
                vcard_text, _etag = source_client.get_contact(f"{source_path}/{filename}")
                dest_client.put_contact(dest_path, filename, vcard_text)
                copied += 1
                log_event(
                    "copy",
                    profile_id=source_profile_id,
                    collection_path=source_path,
                    contact_filename=filename,
                    details={"dest_profile_id": dest_profile_id, "dest_path": dest_path},
                )
            except Exception as exc:
                failed.append(f"{filename}: {exc}")
        update_cached_contact_count(dest_profile_id, dest_path, None)
        if failed:
            flash(f"Copied {copied} contacts. Failed: {'; '.join(failed)}", "error")
        else:
            flash(f"Copied {copied} contacts.", "success")
        return redirect(url_for("global_contacts"))

    flash("Choose a bulk action.", "error")
    return redirect(url_for("global_contacts"))


@app.route('/health')
def health():
    """Basic health endpoint for liveness checks."""
    uptime = datetime.now(timezone.utc) - START_TIME
    return jsonify(
        status="healthy",
        version=app.config.get("APP_VERSION"),
        git_commit=app.config.get("GIT_COMMIT"),
        uptime_seconds=int(uptime.total_seconds()),
    )


@app.route('/ready')
def ready():
    """Readiness endpoint verifying DB and credential store availability."""
    details = {}
    overall = "ready"
    # DB / credential store check
    try:
        # Try a simple read from credential store
        _ = credential_store.get_profiles()
        details["credential_store"] = {"status": "ok"}
    except Exception as exc:
        overall = "degraded"
        details["credential_store"] = {"status": "error", "error": str(exc)}

    # Config checks
    secret_set = bool(app.config.get("PROFILE_SECRET"))
    details["profile_secret_configured"] = secret_set

    if not secret_set:
        # Not fatal — just a warning
        details["note"] = "PROFILE_SECRET_KEY not configured; credentials may be stored unencrypted"

    return jsonify(status=overall, details=details)


@app.route('/debug/profiles')
def debug_profiles():
    """Temporary debug endpoint to list stored connection profiles."""
    try:
        return jsonify(credential_store.get_profiles())
    except Exception as exc:
        app.logger.exception("Failed to return debug profiles")
        return jsonify({"error": str(exc)}), 500


@app.route("/import", methods=["GET", "POST"])
def import_vcf():
    """Import contacts from an uploaded VCF into a selected saved address book."""
    app.logger.debug("Import page requested via %s", request.method)
    targets = build_import_targets()
    selected_target = request.form.get("target") or request.args.get("target", "")
    results = None

    if request.method == "POST":
        selected_target = request.form.get("target", "").strip()
        file = request.files.get("vcf_file")
        app.logger.debug("Import POST target=%s file_present=%s", selected_target, bool(file))
        if not selected_target:
            flash("Please select an address book.", "error")
        elif not file:
            flash("Please choose a VCF file to import.", "error")
        else:
            try:
                profile_id_text, collection_path = selected_target.split("::", 1)
                profile_id = int(profile_id_text)
                results = import_vcf_into_book(profile_id, collection_path, file)
                flash("Import completed.", "success")
            except Exception as exc:
                app.logger.exception("Import failed")
                flash(f"Import failed: {exc}", "error")

    return render_template(
        "import.html",
        title="Import VCF",
        targets=targets,
        selected_target=selected_target,
        results=results,
    )


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts")
def profile_view_contacts(profile_id, collection_path):
    app.logger.debug("Profile view contacts %s %s", profile_id, collection_path)
    try:
        client = get_client_for_profile(profile_id)
    except Exception as exc:
        app.logger.exception("Failed to build client for profile %s", profile_id)
        flash(f"Unable to access profile: {exc}", "error")
        return redirect(url_for("connections"))
    try:
        # find book display name from cache
        books = credential_store.get_cached_address_books(profile_id)
        book = normalize_book_for_template(
            next((b for b in books if b["path"] == collection_path), None),
            fallback_path=collection_path,
        )
        contacts = []
        for item in client.list_contacts(collection_path):
            try:
                parsed = vcard_to_dict(item["vcard"])
                parsed["filename"] = get_contact_filename(item["href"])
                contacts.append(parsed)
            except Exception as exc:
                app.logger.warning(
                    "Skipping invalid contact %s in %s: %s",
                    item.get("href"),
                    collection_path,
                    exc,
                )
                continue
        contacts.sort(
            key=lambda contact: (
                (contact.get("full_name") or "").casefold(),
                (contact.get("organization") or "").casefold(),
                (contact.get("filename") or "").casefold(),
            )
        )
        stats = {
            "total": len(contacts),
            "with_email": sum(1 for contact in contacts if contact.get("emails")),
            "with_phone": sum(1 for contact in contacts if contact.get("phones")),
            "missing_name": sum(1 for contact in contacts if not (contact.get("full_name") or "").strip()),
            "last_modified": book.get("last_seen_at"),
        }
        credential_store.update_connection_status(profile_id, True)
        update_cached_contact_count(profile_id, collection_path, len(contacts))
        bulk_destinations = build_contact_destinations(
            exclude_profile_id=profile_id,
            exclude_collection_path=collection_path,
        )
        return render_template(
            "contacts.html",
            title=f"Contacts - {book.get('display_name')}",
            book=book,
            contacts=contacts,
            profile_id=profile_id,
            bulk_destinations=bulk_destinations,
            stats=stats,
        )
    except Exception as exc:
        app.logger.exception("Unable to load contacts for profile %s book %s", profile_id, collection_path)
        flash(f"Unable to load contacts: {exc}", "error")
        return redirect(url_for("dashboard"))


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts/bulk", methods=["POST"])
def profile_bulk_contacts(profile_id, collection_path):
    action = request.form.get("bulk_action")
    filenames = [name for name in request.form.getlist("contact_filename") if name]
    if not filenames:
        flash("Select at least one contact.", "error")
        return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))

    try:
        client = get_client_for_profile(profile_id)
    except Exception as exc:
        app.logger.exception("Failed to build client for profile %s", profile_id)
        flash(f"Unable to access profile: {exc}", "error")
        return redirect_back_or("connections")

    if action == "delete":
        deleted = 0
        failed = []
        for filename in filenames:
            try:
                client.delete_contact(f"{collection_path.rstrip('/')}/{filename}")
                deleted += 1
            except Exception as exc:
                failed.append(f"{filename}: {exc}")
        update_cached_contact_count(profile_id, collection_path, None)
        for filename in filenames:
            log_event("delete", profile_id=profile_id, collection_path=collection_path, contact_filename=filename)
        if failed:
            flash(f"Deleted {deleted} contacts. Failed: {'; '.join(failed)}", "error")
        else:
            flash(f"Deleted {deleted} contacts.", "success")
        return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))

    if action == "export":
        exported = []
        failed = []
        for filename in filenames:
            try:
                vcard_text, _etag = client.get_contact(f"{collection_path.rstrip('/')}/{filename}")
                exported.append(vcard_text.strip())
            except Exception as exc:
                failed.append(f"{filename}: {exc}")
        if not exported:
            flash(f"Export failed: {'; '.join(failed) or 'no contacts could be exported'}", "error")
            return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))
        if failed:
            flash(f"Exported {len(exported)} contacts. Failed: {'; '.join(failed)}", "error")
        log_event("export", profile_id=profile_id, collection_path=collection_path, details={"scope": "book", "count": len(exported), "failed": len(failed)})
        content = "\n".join(exported) + "\n"
        filename = f"radicale-selected-{datetime.now(timezone.utc).date()}.vcf"
        return send_file(
            BytesIO(content.encode("utf-8")),
            download_name=filename,
            mimetype="text/vcard",
            as_attachment=True,
        )

    if action == "move":
        dest = request.form.get("dest")
        dest_profile_id = None
        dest_path = None
        if dest:
            try:
                parts = dest.split("::", 1)
                dest_profile_id = int(parts[0])
                dest_path = parts[1]
            except Exception:
                dest_profile_id = None
                dest_path = None
        if not dest_profile_id or not dest_path:
            flash("Destination profile and path are required.", "error")
            return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))

        try:
            dest_client = get_client_for_profile(dest_profile_id)
        except Exception as exc:
            app.logger.exception("Failed to build destination client for profile %s", dest_profile_id)
            flash(f"Unable to access destination profile: {exc}", "error")
            return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))

        moved = 0
        failed = []
        for filename in filenames:
            source_href = f"{collection_path.rstrip('/')}/{filename}"
            try:
                vcard_text, _etag = client.get_contact(source_href)
                dest_client.put_contact(dest_path, filename, vcard_text)
                client.delete_contact(source_href)
                moved += 1
                log_event(
                    "move",
                    profile_id=profile_id,
                    collection_path=collection_path,
                    contact_filename=filename,
                    details={"dest_profile_id": dest_profile_id, "dest_path": dest_path},
                )
            except Exception as exc:
                failed.append(f"{filename}: {exc}")

        update_cached_contact_count(profile_id, collection_path, None)
        update_cached_contact_count(dest_profile_id, dest_path, None)
        if failed:
            flash(f"Moved {moved} contacts. Failed: {'; '.join(failed)}", "error")
        else:
            flash(f"Moved {moved} contacts.", "success")
        return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))

    if action == "copy":
        dest = request.form.get("dest")
        dest_profile_id = None
        dest_path = None
        if dest:
            try:
                parts = dest.split("::", 1)
                dest_profile_id = int(parts[0])
                dest_path = parts[1]
            except Exception:
                dest_profile_id = None
                dest_path = None
        if not dest_profile_id or not dest_path:
            flash("Destination profile and path are required.", "error")
            return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))
        try:
            dest_client = get_client_for_profile(dest_profile_id)
        except Exception as exc:
            app.logger.exception("Failed to build destination client for profile %s", dest_profile_id)
            flash(f"Unable to access destination profile: {exc}", "error")
            return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))

        copied = 0
        failed = []
        for filename in filenames:
            source_href = f"{collection_path.rstrip('/')}/{filename}"
            try:
                vcard_text, _etag = client.get_contact(source_href)
                dest_client.put_contact(dest_path, filename, vcard_text)
                copied += 1
                log_event(
                    "copy",
                    profile_id=profile_id,
                    collection_path=collection_path,
                    contact_filename=filename,
                    details={"dest_profile_id": dest_profile_id, "dest_path": dest_path},
                )
            except Exception as exc:
                failed.append(f"{filename}: {exc}")

        update_cached_contact_count(dest_profile_id, dest_path, None)
        if failed:
            flash(f"Copied {copied} contacts. Failed: {'; '.join(failed)}", "error")
        else:
            flash(f"Copied {copied} contacts.", "success")
        return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))

    flash("Choose a bulk action.", "error")
    return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts/quality")
def profile_contact_quality(profile_id, collection_path):
    try:
        client = get_client_for_profile(profile_id)
    except Exception as exc:
        app.logger.exception("Failed to build client for profile %s", profile_id)
        flash(f"Unable to access profile: {exc}", "error")
        return redirect_back_or("connections")
    try:
        profile = credential_store.get_profile(profile_id) or {}
        books = credential_store.get_cached_address_books(profile_id)
        book = normalize_book_for_template(
            next((b for b in books if b["path"] == collection_path), None),
            fallback_path=collection_path,
        )
        contacts = []
        for item in client.list_contacts(collection_path):
            try:
                parsed = vcard_to_dict(item["vcard"])
                parsed["filename"] = get_contact_filename(item["href"])
                parsed["profile_id"] = profile_id
                parsed["profile_name"] = profile.get("name")
                parsed["book_path"] = book.get("path")
                parsed["book_name"] = book.get("display_name")
                contacts.append(parsed)
            except Exception as exc:
                app.logger.warning("Skipping invalid contact during quality scan: %s", exc)
        report = build_duplicate_report(contacts)
        report["duplicates"] = attach_queue_stage_to_duplicate_groups(
            report.get("duplicates") or [],
            scope="book",
            profile_id=profile_id,
            collection_path=book.get("path") or collection_path,
        )
        credential_store.update_connection_status(profile_id, True)
        log_event(
            "quality_scan",
            profile_id=profile_id,
            collection_path=collection_path,
            details={
                "contacts_scanned": len(contacts),
                "duplicate_groups": len(report.get("duplicates") or []),
                "issues": len(report.get("issues") or []),
                "warnings": len(report.get("warnings") or []),
            },
        )
        return render_template(
            "contact_quality.html",
            title=f"Quality - {book.get('display_name')}",
            book=book,
            profile_id=profile_id,
            contacts=contacts,
            report=report,
            quality_actions=QUALITY_DUPLICATE_ACTIONS,
            quality_return_to=request.full_path.rstrip("?"),
        )
    except Exception as exc:
        app.logger.exception("Unable to scan contacts for profile %s book %s", profile_id, collection_path)
        flash(f"Unable to scan contacts: {exc}", "error")
        return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts/new", methods=["GET", "POST"])
def profile_new_contact(profile_id, collection_path):
    app.logger.debug("Profile new contact %s %s", profile_id, collection_path)
    try:
        client = get_client_for_profile(profile_id)
    except Exception as exc:
        app.logger.exception("Failed to build client for profile %s", profile_id)
        flash(f"Unable to access profile: {exc}", "error")
        return redirect_back_or("connections")

    books = credential_store.get_cached_address_books(profile_id)
    book = normalize_book_for_template(
        next((b for b in books if b["path"] == collection_path), None),
        fallback_path=collection_path,
    )
    fields = empty_contact_fields()
    raw_vcard = ""

    if request.method == "POST":
        if request.form.get("save_mode") == "raw":
            raw_vcard = request.form.get("raw_vcard", "").strip()
            if not raw_vcard:
                flash("Raw vCard cannot be empty.", "error")
                return render_template(
                    "edit_contact.html",
                    title="New Contact",
                    form_title="New Contact",
                    book=book,
                    contact={"filename": "new contact"},
                    fields=fields,
                    raw_vcard=raw_vcard,
                    profile_id=profile_id,
                    is_new=True,
                )
            try:
                fields = vcard_to_dict(raw_vcard)
                filename = f"{fields.get('uid') or uuid.uuid4()}.vcf"
                client.put_contact(collection_path, filename, raw_vcard + "\n")
                update_cached_contact_count(profile_id, collection_path, None)
                log_event("create", profile_id=profile_id, collection_path=collection_path, contact_filename=filename, details={"mode": "raw"})
                flash("Contact created.", "success")
                return redirect(url_for("profile_view_contact", profile_id=profile_id, collection_path=collection_path, contact_filename=filename))
            except Exception as exc:
                app.logger.exception("Unable to create raw contact")
                flash(f"Unable to create contact: {exc}", "error")
                return render_template(
                    "edit_contact.html",
                    title="New Contact",
                    form_title="New Contact",
                    book=book,
                    contact={"filename": "new contact"},
                    fields=fields,
                    raw_vcard=raw_vcard,
                    profile_id=profile_id,
                    is_new=True,
                )

        fields = contact_fields_from_form()
        if not fields["full_name"] and not fields["first_name"] and not fields["last_name"]:
            flash("Enter at least a full name, first name, or last name.", "error")
            raw_vcard = build_vcard_from_fields(fields)
            return render_template(
                "edit_contact.html",
                title="New Contact",
                form_title="New Contact",
                book=book,
                contact={"filename": "new contact"},
                fields=fields,
                raw_vcard=raw_vcard,
                profile_id=profile_id,
                is_new=True,
            )
        try:
            fields["uid"] = fields.get("uid") or str(uuid.uuid4())
            if not fields["full_name"]:
                fields["full_name"] = " ".join(part for part in [fields["first_name"], fields["last_name"]] if part)
            filename = f"{fields['uid']}.vcf"
            new_vcard = build_vcard_from_fields(fields)
            client.put_contact(collection_path, filename, new_vcard)
            update_cached_contact_count(profile_id, collection_path, None)
            log_event("create", profile_id=profile_id, collection_path=collection_path, contact_filename=filename, details={"mode": "structured"})
            flash("Contact created.", "success")
            return redirect(url_for("profile_view_contact", profile_id=profile_id, collection_path=collection_path, contact_filename=filename))
        except Exception as exc:
            app.logger.exception("Unable to create contact")
            flash(f"Unable to create contact: {exc}", "error")
            raw_vcard = build_vcard_from_fields(fields)

    return render_template(
        "edit_contact.html",
        title="New Contact",
        form_title="New Contact",
        book=book,
        contact={"filename": "new contact"},
        fields=fields,
        raw_vcard=raw_vcard,
        profile_id=profile_id,
        is_new=True,
    )


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts/<contact_filename>")
def profile_view_contact(profile_id, collection_path, contact_filename):
    app.logger.debug("Profile view contact %s %s %s", profile_id, collection_path, contact_filename)
    try:
        client = get_client_for_profile(profile_id)
    except Exception as exc:
        app.logger.exception("Failed to build client for profile %s", profile_id)
        flash(f"Unable to access profile: {exc}", "error")
        return redirect(url_for("connections"))
    try:
        books = credential_store.get_cached_address_books(profile_id)
        book = normalize_book_for_template(
            next((b for b in books if b["path"] == collection_path), None),
            fallback_path=collection_path,
        )
        contact_href = f"{collection_path.rstrip('/')}/{contact_filename}"
        vcard_text, etag = client.get_contact(contact_href)
        contact = parse_vcard_contact(vcard_text)
        contact["filename"] = contact_filename
        contact["etag"] = etag
        contact["href"] = contact_href
        credential_store.update_connection_status(profile_id, True)
        destinations = build_contact_destinations(
            exclude_profile_id=profile_id,
            exclude_collection_path=collection_path,
        )
        return render_template(
            "contact_detail.html",
            title=f"Contact - {contact_filename}",
            book=book,
            contact=contact,
            profile_id=profile_id,
            destinations=destinations,
        )
    except Exception as exc:
        app.logger.exception("Unable to load contact %s", contact_filename)
        flash(f"Unable to load contact: {exc}", "error")
        return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts/<contact_filename>/transfer")
def profile_contact_transfer(profile_id, collection_path, contact_filename):
    action = (request.args.get("action") or "copy").strip().lower()
    if action not in ("copy", "move"):
        action = "copy"
    try:
        client = get_client_for_profile(profile_id)
        books = credential_store.get_cached_address_books(profile_id)
        book = normalize_book_for_template(
            next((b for b in books if b["path"] == collection_path), None),
            fallback_path=collection_path,
        )
        contact_href = f"{collection_path.rstrip('/')}/{contact_filename}"
        vcard_text, _etag = client.get_contact(contact_href)
        contact = parse_vcard_contact(vcard_text)
        contact["filename"] = contact_filename
        destinations = build_contact_destinations(
            exclude_profile_id=profile_id,
            exclude_collection_path=collection_path,
        )
        return render_template(
            "contact_transfer.html",
            title=f"{action.title()} Contact",
            action=action,
            profile_id=profile_id,
            book=book,
            contact=contact,
            destinations=destinations,
            contact_filename=contact_filename,
        )
    except Exception as exc:
        app.logger.exception("Unable to load transfer page for contact %s", contact_filename)
        flash(f"Unable to load transfer page: {exc}", "error")
        return redirect(
            url_for(
                "profile_view_contact",
                profile_id=profile_id,
                collection_path=collection_path,
                contact_filename=contact_filename,
            )
        )


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/export")
def profile_export_addressbook(profile_id, collection_path):
    app.logger.debug("Profile export requested %s %s", profile_id, collection_path)
    try:
        client = get_client_for_profile(profile_id)
    except Exception as exc:
        app.logger.exception("Failed to build client for profile %s", profile_id)
        flash(f"Unable to access profile: {exc}", "error")
        return redirect(url_for("connections"))
    try:
        content = client.export_addressbook(collection_path)
        if not content:
            flash("No contacts available to export.", "error")
            return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))
        filename = f"radicale-{collection_path.replace('/', '_')}-{datetime.now(timezone.utc).date()}.vcf"
        log_event("addressbook_export", profile_id=profile_id, collection_path=collection_path)
        buffer = BytesIO(content.encode("utf-8"))
        return send_file(buffer, download_name=filename, mimetype="text/vcard", as_attachment=True)
    except Exception as exc:
        app.logger.exception("Export failed for profile %s book %s", profile_id, collection_path)
        flash(f"Export failed: {exc}", "error")
        return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts/<contact_filename>/export")
def profile_export_contact(profile_id, collection_path, contact_filename):
    try:
        client = get_client_for_profile(profile_id)
        vcard_text, _etag = client.get_contact(f"{collection_path.rstrip('/')}/{contact_filename}")
        log_event("contact_export", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename)
        return send_file(
            BytesIO((vcard_text.strip() + "\n").encode("utf-8")),
            download_name=contact_filename,
            mimetype="text/vcard",
            as_attachment=True,
        )
    except Exception as exc:
        app.logger.exception("Contact export failed")
        flash(f"Contact export failed: {exc}", "error")
        return redirect(url_for("profile_view_contact", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename))


@app.route("/profiles/<int:profile_id>/export")
def profile_export_connection(profile_id):
    profile = credential_store.get_profile(profile_id)
    if not profile:
        flash("Profile not found.", "error")
        return redirect_back_or("connections")
    try:
        client = get_client_for_profile(profile_id)
    except Exception as exc:
        flash(f"Unable to access profile: {exc}", "error")
        return redirect_back_or("connections")
    books = credential_store.get_cached_address_books(profile_id) or []
    blocks = []
    failed = []
    for book in books:
        path = book.get("path")
        if not path:
            continue
        try:
            content = client.export_addressbook(path)
            if content.strip():
                blocks.append(content.strip())
        except Exception as exc:
            failed.append(f"{path}: {exc}")
    if not blocks:
        flash(f"No contacts exported. {'; '.join(failed)}", "error")
        return redirect(url_for("connections"))
    if failed:
        flash(f"Exported connection with warnings: {'; '.join(failed)}", "error")
    log_event("connection_export", profile_id=profile_id, details={"books": len(books), "failed": len(failed)})
    content = "\n".join(blocks) + "\n"
    return send_file(
        BytesIO(content.encode("utf-8")),
        download_name=f"radicale-{profile.get('name', 'connection')}-{datetime.now(timezone.utc).date()}.vcf",
        mimetype="text/vcard",
        as_attachment=True,
    )


@app.route("/profiles/<int:profile_id>/backup")
def profile_backup_export(profile_id):
    profile = credential_store.get_profile(profile_id)
    if not profile:
        flash("Profile not found.", "error")
        return redirect(url_for("connections"))
    try:
        client = get_client_for_profile(profile_id)
    except Exception as exc:
        flash(f"Unable to access profile: {exc}", "error")
        return redirect(url_for("connections"))

    books = credential_store.get_cached_address_books(profile_id) or []
    archive = BytesIO()
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "profile": {
            "id": profile.get("id"),
            "name": profile.get("name"),
            "server_url": profile.get("server_url"),
            "username": profile.get("username"),
            "group_name": profile.get("group_name", ""),
            "tags": profile.get("tags", []),
        },
        "books": [],
    }
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for book in books:
            normalized = normalize_book_for_template(book)
            path = normalized["path"]
            safe_name = path.replace("/", "_")
            try:
                exported = client.export_addressbook(path)
                if exported.strip():
                    zf.writestr(f"AddressBooks/{safe_name}.vcf", exported)
                listed = client.list_contacts(path)
                contact_files = []
                for item in listed:
                    filename = get_contact_filename(item.get("href")) or f"{uuid.uuid4()}.vcf"
                    contact_files.append(filename)
                    zf.writestr(f"Contacts/{safe_name}/{filename}", item["vcard"].strip() + "\n")
                metadata["books"].append(
                    {
                        "display_name": normalized["display_name"],
                        "path": path,
                        "contact_count": len(contact_files),
                        "files": contact_files,
                        "last_seen_at": normalized.get("last_seen_at"),
                    }
                )
            except Exception as exc:
                metadata["books"].append(
                    {"display_name": normalized["display_name"], "path": path, "error": str(exc)}
                )
        zf.writestr("Metadata/connection.json", json.dumps(metadata, indent=2))
        zf.writestr(
            "Metadata/recent_events.json",
            json.dumps(credential_store.get_recent_events(limit=200), indent=2),
        )
    archive.seek(0)
    log_event("backup_export", profile_id=profile_id, details={"books": len(books)})
    return send_file(
        archive,
        download_name=f"radicale-backup-{profile.get('name', 'connection')}-{datetime.now(timezone.utc).date()}.zip",
        mimetype="application/zip",
        as_attachment=True,
    )


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts/<contact_filename>/edit", methods=["GET", "POST"])
def profile_edit_contact(profile_id, collection_path, contact_filename):
    app.logger.debug("Profile edit contact %s %s %s", profile_id, collection_path, contact_filename)
    try:
        client = get_client_for_profile(profile_id)
    except Exception as exc:
        app.logger.exception("Failed to build client for profile %s", profile_id)
        flash(f"Unable to access profile: {exc}", "error")
        return redirect(url_for("connections"))
    contact_href = f"{collection_path.rstrip('/')}/{contact_filename}"
    try:
        books = credential_store.get_cached_address_books(profile_id)
        book = normalize_book_for_template(
            next((b for b in books if b["path"] == collection_path), None),
            fallback_path=collection_path,
        )
        vcard_text, etag = client.get_contact(contact_href)
        fields = vcard_to_dict(vcard_text)
        if request.method == "POST":
            if request.form.get("save_mode") == "raw":
                raw_vcard = request.form.get("raw_vcard", "").strip()
                if not raw_vcard:
                    flash("Raw vCard cannot be empty.", "error")
                    return render_template(
                        "edit_contact.html",
                        title="Edit Contact",
                        book=book,
                        contact={"filename": contact_filename},
                        fields=fields,
                        raw_vcard=vcard_text,
                        profile_id=profile_id,
                    )
                client.put_contact(collection_path, contact_filename, raw_vcard + "\n", if_match=etag)
                log_event("edit", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename, details={"mode": "raw"})
                flash("Contact saved.", "success")
                return redirect(url_for("profile_view_contact", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename))
            values = contact_fields_from_form(fields.get("uid"))
            values["uid"] = fields.get("uid") or values.get("uid")
            new_vcard = build_vcard_from_fields(values)
            new_vcard = merge_unknown_fields_into_vcard(vcard_text, new_vcard)
            client.put_contact(collection_path, contact_filename, new_vcard, if_match=etag)
            log_event("edit", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename)
            flash("Contact saved.", "success")
            return redirect(url_for("profile_view_contact", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename))
        return render_template(
            "edit_contact.html",
            title="Edit Contact",
            book=book,
            contact={"filename": contact_filename},
            fields=fields,
            raw_vcard=vcard_text,
            profile_id=profile_id,
        )
    except Exception as exc:
        app.logger.exception("Unable to edit contact %s", contact_href)
        flash(f"Unable to edit contact: {exc}", "error")
        return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts/<contact_filename>/delete", methods=["POST"])
def profile_delete_contact(profile_id, collection_path, contact_filename):
    app.logger.debug("Profile delete contact %s %s %s", profile_id, collection_path, contact_filename)
    try:
        client = get_client_for_profile(profile_id)
    except Exception as exc:
        app.logger.exception("Failed to build client for profile %s", profile_id)
        flash(f"Unable to access profile: {exc}", "error")
        return redirect(url_for("connections"))
    contact_href = f"{collection_path.rstrip('/')}/{contact_filename}"
    try:
        client.delete_contact(contact_href)
        log_event("delete", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename)
        flash("Contact deleted.", "success")
    except Exception as exc:
        app.logger.exception("Delete failed for contact %s", contact_href)
        flash(f"Delete failed: {exc}", "error")
    return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/import", methods=["GET", "POST"])
def profile_import_vcf(profile_id, collection_path):
    app.logger.debug("Profile import %s %s %s", profile_id, collection_path, request.method)
    try:
        get_client_for_profile(profile_id)
    except Exception as exc:
        app.logger.exception("Failed to build client for profile %s", profile_id)
        flash(f"Unable to access profile: {exc}", "error")
        return redirect(url_for("connections"))
    results = None
    books = [
        normalize_book_for_template(book)
        for book in (credential_store.get_cached_address_books(profile_id) or [])
    ]
    if request.method == "POST":
        selected_path = request.form.get("collection_path") or collection_path
        try:
            results = import_vcf_into_book(profile_id, selected_path, request.files.get("vcf_file"))
            flash("Import completed.", "success")
        except Exception as exc:
            app.logger.exception("Import failed for profile %s", profile_id)
            flash(f"Import failed: {exc}", "error")
    return render_template(
        "import.html",
        title="Import VCF",
        books=books,
        selected_path=collection_path,
        results=results,
    )


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts/<contact_filename>/copy", methods=["POST"])
def profile_copy_contact(profile_id, collection_path, contact_filename):
    dest = request.form.get("dest")
    dest_profile_id = None
    dest_path = None
    if dest:
        try:
            parts = dest.split("::", 1)
            dest_profile_id = int(parts[0])
            dest_path = parts[1]
        except Exception:
            dest_profile_id = None
            dest_path = None
    if not dest_profile_id or not dest_path:
        flash("Destination profile and path are required.", "error")
        return redirect(url_for("profile_view_contact", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename))
    try:
        src_client = get_client_for_profile(profile_id)
        vcard_text, etag = src_client.get_contact(f"{collection_path.rstrip('/')}/{contact_filename}")
        dest_client = get_client_for_profile(dest_profile_id)
        dest_client.put_contact(dest_path, contact_filename, vcard_text)
        update_cached_contact_count(dest_profile_id, dest_path, None)
        log_event(
            "copy",
            profile_id=profile_id,
            collection_path=collection_path,
            contact_filename=contact_filename,
            details={"dest_profile_id": dest_profile_id, "dest_path": dest_path},
        )
        flash("Contact copied successfully.", "success")
    except Exception as exc:
        app.logger.exception("Copy failed")
        flash(f"Copy failed: {exc}", "error")
    return redirect(url_for("profile_view_contact", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename))


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts/<contact_filename>/duplicate", methods=["POST"])
def profile_duplicate_contact(profile_id, collection_path, contact_filename):
    try:
        client = get_client_for_profile(profile_id)
        vcard_text, _etag = client.get_contact(f"{collection_path.rstrip('/')}/{contact_filename}")
        new_uid, copied_vcard = duplicate_vcard(vcard_text)
        new_filename = f"{new_uid}.vcf"
        client.put_contact(collection_path, new_filename, copied_vcard)
        update_cached_contact_count(profile_id, collection_path, None)
        log_event(
            "duplicate",
            profile_id=profile_id,
            collection_path=collection_path,
            contact_filename=new_filename,
            details={"source_filename": contact_filename},
        )
        flash("Contact duplicated.", "success")
        return redirect(
            url_for(
                "profile_view_contact",
                profile_id=profile_id,
                collection_path=collection_path,
                contact_filename=new_filename,
            )
        )
    except Exception as exc:
        app.logger.exception("Duplicate failed")
        flash(f"Duplicate failed: {exc}", "error")
        return redirect(url_for("profile_view_contact", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename))


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/contacts/<contact_filename>/move", methods=["POST"])
def profile_move_contact(profile_id, collection_path, contact_filename):
    dest = request.form.get("dest")
    dest_profile_id = None
    dest_path = None
    if dest:
        try:
            parts = dest.split("::", 1)
            dest_profile_id = int(parts[0])
            dest_path = parts[1]
        except Exception:
            dest_profile_id = None
            dest_path = None
    if not dest_profile_id or not dest_path:
        flash("Destination profile and path are required.", "error")
        return redirect(url_for("profile_view_contact", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename))
    try:
        src_client = get_client_for_profile(profile_id)
        vcard_text, etag = src_client.get_contact(f"{collection_path.rstrip('/')}/{contact_filename}")
        dest_client = get_client_for_profile(dest_profile_id)
        dest_client.put_contact(dest_path, contact_filename, vcard_text)
        # If put succeeded, delete source
        src_client.delete_contact(f"{collection_path.rstrip('/')}/{contact_filename}")
        update_cached_contact_count(profile_id, collection_path, None)
        update_cached_contact_count(dest_profile_id, dest_path, None)
        log_event(
            "move",
            profile_id=profile_id,
            collection_path=collection_path,
            contact_filename=contact_filename,
            details={"dest_profile_id": dest_profile_id, "dest_path": dest_path},
        )
        flash("Contact moved successfully.", "success")
        return redirect(url_for("profile_view_contact", profile_id=dest_profile_id, collection_path=dest_path, contact_filename=contact_filename))
    except Exception as exc:
        app.logger.exception("Move failed")
        flash(f"Move failed: {exc}", "error")
        return redirect(url_for("profile_view_contact", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename))


@app.route("/books/<path:collection_path>/contacts")
def view_contacts(collection_path):
    """LEGACY: Show all contacts using session-based connection.
    
    DEPRECATED: Use profile_view_contacts() instead, which uses saved profiles.
    This route relies on session['active_connection'] and is maintained for backwards compatibility only.
    """
    app.logger.debug("View contacts for path: %s", collection_path)
    client = make_client()
    if not client:
        app.logger.warning("View contacts denied: no active client")
        flash("Please connect first.", "error")
        return redirect(url_for("login"))

    try:
        books = client.discover_addressbooks()
        book = next((b for b in books if b["path"] == collection_path), None)
        if not book:
            app.logger.warning("Address book not found: %s", collection_path)
            flash("Address book not found.", "error")
            return redirect(url_for("dashboard"))
        contacts = []
        for item in client.list_contacts(collection_path):
            try:
                parsed = vcard_to_dict(item["vcard"])
                parsed["filename"] = get_contact_filename(item["href"])
                contacts.append(parsed)
            except Exception as exc:
                app.logger.warning(
                    "Skipping invalid contact %s in %s: %s",
                    item.get("href"),
                    collection_path,
                    exc,
                )
                continue
        app.logger.debug("Loaded %d contacts for book %s", len(contacts), collection_path)
        return render_template(
            "contacts.html",
            title=f"Contacts - {book['name']}",
            book=book,
            contacts=contacts,
        )
    except Exception as exc:
        app.logger.exception("Unable to load contacts for %s", collection_path)
        flash(f"Unable to load contacts: {exc}", "error")
        return redirect(url_for("dashboard"))


@app.route("/books/<path:collection_path>/contacts/<contact_filename>")
def view_contact(collection_path, contact_filename):
    """LEGACY: Show detail information for a single contact using session-based connection.
    
    DEPRECATED: Use profile_view_contact() instead, which uses saved profiles.
    This route relies on session['active_connection'] and is maintained for backwards compatibility only.
    """
    app.logger.debug("View contact requested: %s/%s", collection_path, contact_filename)
    client = make_client()
    if not client:
        app.logger.warning("View contact denied: no active client")
        flash("Please connect first.", "error")
        return redirect(url_for("login"))

    try:
        books = client.discover_addressbooks()
        book = next((b for b in books if b["path"] == collection_path), None)
        if not book:
            app.logger.warning("Address book not found: %s", collection_path)
            flash("Address book not found.", "error")
            return redirect(url_for("dashboard"))

        contact_href = f"{collection_path.rstrip('/')}/{contact_filename}"
        vcard_text, etag = client.get_contact(contact_href)
        contact = parse_vcard_contact(vcard_text)
        contact["filename"] = contact_filename
        contact["etag"] = etag
        contact["href"] = contact_href

        return render_template(
            "contact_detail.html",
            title=f"Contact - {contact_filename}",
            book=book,
            contact=contact,
        )
    except Exception as exc:
        app.logger.exception("Unable to load contact %s", contact_href)
        flash(f"Unable to load contact: {exc}", "error")
        return redirect(url_for("view_contacts", collection_path=collection_path))


@app.route("/books/<path:collection_path>/export")
def export_addressbook(collection_path):
    """LEGACY: Export the selected address book using session-based connection.
    
    DEPRECATED: Use profile_export_addressbook() instead, which uses saved profiles.
    This route relies on session['active_connection'] and is maintained for backwards compatibility only.
    """
    app.logger.debug("Export requested for path: %s", collection_path)
    client = make_client()
    if not client:
        app.logger.warning("Export denied: no active client")
        flash("Please connect first.", "error")
        return redirect(url_for("login"))

    try:
        content = client.export_addressbook(collection_path)
        if not content:
            app.logger.info("Export found no content for path: %s", collection_path)
            flash("No contacts available to export.", "error")
            return redirect(url_for("view_contacts", collection_path=collection_path))
        filename = f"radicale-{collection_path.replace('/', '_')}-{datetime.now(timezone.utc).date()}.vcf"
        app.logger.info("Exporting address book %s as %s", collection_path, filename)
        buffer = BytesIO(content.encode("utf-8"))
        return send_file(
            buffer,
            download_name=filename,
            mimetype="text/vcard",
            as_attachment=True,
        )
    except Exception as exc:
        app.logger.exception("Export failed for path: %s", collection_path)
        flash(f"Export failed: {exc}", "error")
        return redirect(url_for("view_contacts", collection_path=collection_path))


@app.route("/books/<path:collection_path>/contacts/<contact_filename>/edit", methods=["GET", "POST"])
def edit_contact(collection_path, contact_filename):
    """LEGACY: Edit a contact using session-based connection.
    
    DEPRECATED: Use profile_edit_contact() instead, which uses saved profiles.
    This route relies on session['active_connection'] and is maintained for backwards compatibility only.
    """
    app.logger.debug("Edit contact requested: %s/%s", collection_path, contact_filename)
    client = make_client()
    if not client:
        app.logger.warning("Edit contact denied: no active client")
        flash("Please connect first.", "error")
        return redirect(url_for("login"))

    contact_href = f"{collection_path.rstrip('/')}/{contact_filename}"
    try:
        vcard_text, etag = client.get_contact(contact_href)
        app.logger.debug("Loaded contact %s with ETag=%s", contact_href, etag)
        fields = vcard_to_dict(vcard_text)
        if request.method == "POST":
            values = {
                "uid": fields.get("uid"),
                "full_name": request.form.get("full_name", "").strip(),
                "first_name": request.form.get("first_name", "").strip(),
                "last_name": request.form.get("last_name", "").strip(),
                "organization": request.form.get("organization", "").strip(),
                "note": request.form.get("note", "").strip(),
                "emails": [email.strip() for email in request.form.get("emails", "").split(",") if email.strip()],
                "phones": [phone.strip() for phone in request.form.get("phones", "").split(",") if phone.strip()],
            }
            app.logger.debug("Edit contact POST values for %s: %s", contact_href, {k: values[k] for k in ["full_name", "first_name", "last_name"]})
            values["uid"] = fields.get("uid") or values.get("uid")
            new_vcard = build_vcard_from_fields(values)
            client.put_contact(collection_path, contact_filename, new_vcard, if_match=etag)
            app.logger.info("Contact updated: %s with ETag=%s", contact_href, etag)
            flash("Contact saved.", "success")
            return redirect(url_for("view_contacts", collection_path=collection_path))
        return render_template(
            "edit_contact.html",
            title="Edit Contact",
            book={"path": collection_path, "name": collection_path},
            contact={"filename": contact_filename},
            fields=fields,
        )
    except Exception as exc:
        app.logger.exception("Unable to edit contact %s", contact_href)
        flash(f"Unable to edit contact: {exc}", "error")
        return redirect(url_for("view_contacts", collection_path=collection_path))


@app.route("/books/<path:collection_path>/contacts/<contact_filename>/delete")
def delete_contact(collection_path, contact_filename):
    """LEGACY: Delete a contact using session-based connection.
    
    DEPRECATED: Use profile_delete_contact() instead, which uses saved profiles.
    This route relies on session['active_connection'] and is maintained for backwards compatibility only.
    """
    app.logger.debug("Delete contact requested: %s/%s", collection_path, contact_filename)
    client = make_client()
    if not client:
        app.logger.warning("Delete contact denied: no active client")
        flash("Please connect first.", "error")
        return redirect(url_for("login"))

    contact_href = f"{collection_path.rstrip('/')}/{contact_filename}"
    try:
        app.logger.debug("Deleting contact %s", contact_href)
        client.delete_contact(contact_href)
        app.logger.info("Deleted contact: %s", contact_href)
        flash("Contact deleted.", "success")
    except Exception as exc:
        app.logger.exception("Delete failed for contact %s", contact_href)
        flash(f"Delete failed: {exc}", "error")
    return redirect(url_for("view_contacts", collection_path=collection_path))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes", "on")
    extra_files = [".env"] if debug and os.path.exists(".env") else None
    app.run(host="0.0.0.0", port=port, debug=debug, extra_files=extra_files)

