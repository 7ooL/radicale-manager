import logging
import os
from datetime import datetime
from io import BytesIO
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
)
from radicale_client import RadicaleClient
from contact_utils import (
    parse_vcf_contacts,
    parse_vcard_contact,
    vcard_to_dict,
    build_vcard_from_fields,
    get_contact_filename,
)
from credential_store import CredentialStore


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


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("APP_SECRET_KEY") or os.urandom(24)
    app.config["PROFILE_STORE_PATH"] = os.environ.get(
        "PROFILE_STORE_PATH",
        "/data/profiles.db",
    )
    app.config["DEFAULT_RADICALE_URL"] = os.environ.get(
        "DEFAULT_RADICALE_URL",
        "https://radicale.murrey.io",
    )
    app.config["PROFILE_SECRET"] = os.environ.get("PROFILE_SECRET_KEY")
    app.config["APP_VERSION"] = os.environ.get("APP_VERSION", "0.1.0")
    app.config["GIT_COMMIT"] = os.environ.get("GIT_COMMIT")
    configure_logging(app)
    return app


app = create_app()
credential_store = CredentialStore(app.config["PROFILE_STORE_PATH"], app.config["PROFILE_SECRET"])

# record process start for uptime
START_TIME = datetime.utcnow()


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
                flash("No address books found on the server.", "error")
                return render_template("login.html", title="Login", profiles=profiles, form=form)

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
                    app.logger.warning("PROFILE CREATED id=%s", profile_id)
                    app.logger.warning(
                        "PROFILES AFTER CREATE: %s",
                        credential_store.get_profiles(),
                    )
                except Exception:
                    app.logger.exception("Failed to create profile %s", profile_name)

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
            p["books"] = credential_store.get_cached_address_books(p["id"]) or []
        except Exception:
            p["books"] = []
    return render_template("connections.html", title="Connections", profiles=profiles)


@app.route("/connections/new", methods=["GET", "POST"])
def new_connection():
    app.logger.debug("New connection page %s", request.method)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        server_url = request.form.get("server_url", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        enabled = bool(request.form.get("enabled"))
        if not name or not server_url or not username:
            flash("Name, server URL and username are required.", "error")
            return render_template("connections_new.html", form=request.form)
        try:
            profile_id = credential_store.create_profile(name, server_url, username, password=password or None, enabled=enabled)
            flash("Connection saved.", "success")
            return redirect(url_for("connections"))
        except Exception as exc:
            app.logger.exception("Failed to create profile")
            flash(f"Failed to save connection: {exc}", "error")
            return render_template("connections_new.html", form=request.form)
    return render_template("connections_new.html", form={})


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
        credential_store.save_or_update_address_books(profile_id, books)
        credential_store.update_connection_status(profile_id, True)
        flash(f"Connection test succeeded: found {len(books)} books.", "success")
    except Exception as exc:
        credential_store.update_connection_status(profile_id, False, str(exc))
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
        credential_store.save_or_update_address_books(profile_id, books)
        credential_store.update_connection_status(profile_id, True)
        flash(f"Refreshed {len(books)} address books.", "success")
    except Exception as exc:
        credential_store.update_connection_status(profile_id, False, str(exc))
        app.logger.exception("Refresh failed for %s", profile_id)
        flash(f"Refresh failed: {exc}", "error")
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
            books = credential_store.get_cached_address_books(p["id"]) or []
            # If no cache, try to discover now if password available
            if not books and p.get("password") is None:
                # load profile to get decrypted password
                full = credential_store.get_profile(p["id"]) or {}
                p_password = full.get("password")
            else:
                p_password = None
            if not books and p_password:
                try:
                    client = RadicaleClient(p["server_url"], p["username"], p_password)
                    discovered = client.discover_addressbooks()
                    credential_store.save_or_update_address_books(p["id"], discovered)
                    books = credential_store.get_cached_address_books(p["id"]) or []
                except Exception as exc:
                    app.logger.warning("Discovery for profile %s failed: %s", p.get("name"), exc)
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
        "duplicates": 0,
        "issues": 0,
    }

    return render_template("dashboard.html", title="Dashboard", profiles=display, metrics=metrics)


@app.route('/health')
def health():
    """Basic health endpoint for liveness checks."""
    uptime = datetime.utcnow() - START_TIME
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
    """Import contacts from an uploaded VCF into a selected Radicale address book."""
    app.logger.debug("Import page requested via %s", request.method)
    client = make_client()
    if not client:
        flash("Please connect first.", "error")
        return redirect(url_for("login"))

    books = client.discover_addressbooks()
    selected_path = request.args.get("target", "")
    results = None

    if request.method == "POST":
        selected_path = request.form.get("collection_path", "").strip()
        file = request.files.get("vcf_file")
        app.logger.debug("Import POST selected_path=%s file_present=%s", selected_path, bool(file))
        if not selected_path or not file:
            app.logger.warning("Import POST missing path or file")
            flash("Please select an address book and upload a VCF file.", "error")
            return render_template("import.html", title="Import VCF", books=books, selected_path=selected_path)

        try:
            raw_bytes = file.read()
            app.logger.debug("Read upload file size=%d", len(raw_bytes))
            content = raw_bytes.decode("utf-8", errors="replace")
            contacts, failed = parse_vcf_contacts(content)
            app.logger.debug("Parsed %d contacts with %d failed blocks", len(contacts), len(failed))
            processed = 0
            created = 0
            updated = 0
            for contact in contacts:
                processed += 1
                try:
                    response = client.put_contact(selected_path, contact["filename"], contact["vcard"])
                    if response.status_code == 201:
                        created += 1
                    else:
                        updated += 1
                except Exception as exc:
                    app.logger.exception("Failed to upload contact %s", contact.get("filename"))
                    failed.append({"error": f"Failed to upload {contact['filename']}: {exc}"})
            results = {"processed": processed, "created": created, "updated": updated, "failed": failed}
            app.logger.info(
                "Import completed path=%s processed=%d created=%d updated=%d failed=%d",
                selected_path,
                processed,
                created,
                updated,
                len(failed),
            )
            flash("Import completed.", "success")
        except UnicodeDecodeError as exc:
            app.logger.exception("VCF decode failed")
            flash(f"Import failed: cannot decode uploaded file as UTF-8: {exc}", "error")
        except Exception as exc:
            app.logger.exception("Import failed during parsing or upload")
            flash(f"Import failed: {exc}", "error")

    return render_template(
        "import.html",
        title="Import VCF",
        books=books,
        selected_path=selected_path,
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
        book = next((b for b in books if b["path"] == collection_path), {"path": collection_path, "display_name": collection_path})
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
        return render_template(
            "contacts.html",
            title=f"Contacts - {book.get('display_name')}",
            book={"path": collection_path, "name": book.get("display_name")},
            contacts=contacts,
            profile_id=profile_id,
        )
    except Exception as exc:
        app.logger.exception("Unable to load contacts for profile %s book %s", profile_id, collection_path)
        flash(f"Unable to load contacts: {exc}", "error")
        return redirect(url_for("dashboard"))


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
        book = next((b for b in books if b["path"] == collection_path), {"path": collection_path, "display_name": collection_path})
        contact_href = f"{collection_path.rstrip('/')}/{contact_filename}"
        vcard_text, etag = client.get_contact(contact_href)
        contact = parse_vcard_contact(vcard_text)
        contact["filename"] = contact_filename
        contact["etag"] = etag
        contact["href"] = contact_href
        # prepare destination options (enabled profiles and their books)
        destinations = []
        try:
            enabled = credential_store.get_enabled_profiles()
            for pp in enabled:
                books_list = credential_store.get_cached_address_books(pp["id"]) or []
                for b in books_list:
                    destinations.append({"profile_id": pp["id"], "profile_name": pp["name"], "path": b["path"], "display": f"{pp['name']} / {b['display_name']}"})
        except Exception:
            destinations = []
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
        filename = f"radicale-{collection_path.replace('/', '_')}-{datetime.utcnow().date()}.vcf"
        buffer = BytesIO(content.encode("utf-8"))
        return send_file(buffer, download_name=filename, mimetype="text/vcard", as_attachment=True)
    except Exception as exc:
        app.logger.exception("Export failed for profile %s book %s", profile_id, collection_path)
        flash(f"Export failed: {exc}", "error")
        return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))


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
        vcard_text, etag = client.get_contact(contact_href)
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
            values["uid"] = fields.get("uid") or values.get("uid")
            new_vcard = build_vcard_from_fields(values)
            client.put_contact(collection_path, contact_filename, new_vcard, if_match=etag)
            flash("Contact saved.", "success")
            return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))
        return render_template(
            "edit_contact.html",
            title="Edit Contact",
            book={"path": collection_path, "name": collection_path},
            contact={"filename": contact_filename},
            fields=fields,
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
        flash("Contact deleted.", "success")
    except Exception as exc:
        app.logger.exception("Delete failed for contact %s", contact_href)
        flash(f"Delete failed: {exc}", "error")
    return redirect(url_for("profile_view_contacts", profile_id=profile_id, collection_path=collection_path))


@app.route("/profiles/<int:profile_id>/books/<path:collection_path>/import", methods=["GET", "POST"])
def profile_import_vcf(profile_id, collection_path):
    app.logger.debug("Profile import %s %s %s", profile_id, collection_path, request.method)
    try:
        client = get_client_for_profile(profile_id)
    except Exception as exc:
        app.logger.exception("Failed to build client for profile %s", profile_id)
        flash(f"Unable to access profile: {exc}", "error")
        return redirect(url_for("connections"))
    results = None
    if request.method == "POST":
        file = request.files.get("vcf_file")
        if not file:
            flash("Please upload a VCF file.", "error")
            return render_template("import.html", title="Import VCF", books=[], selected_path=collection_path)
        try:
            content = file.read().decode("utf-8", errors="replace")
            contacts, failed = parse_vcf_contacts(content)
            processed = created = updated = 0
            for contact in contacts:
                try:
                    response = client.put_contact(collection_path, contact["filename"], contact["vcard"])
                    if response.status_code == 201:
                        created += 1
                    else:
                        updated += 1
                    processed += 1
                except Exception as exc:
                    failed.append({"error": str(exc), "filename": contact.get("filename")})
            results = {"processed": processed, "created": created, "updated": updated, "failed": failed}
            flash("Import completed.", "success")
        except Exception as exc:
            app.logger.exception("Import failed for profile %s", profile_id)
            flash(f"Import failed: {exc}", "error")
    return render_template("import.html", title="Import VCF", books=credential_store.get_cached_address_books(profile_id), selected_path=collection_path, results=results)


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
        flash("Contact copied successfully.", "success")
    except Exception as exc:
        app.logger.exception("Copy failed")
        flash(f"Copy failed: {exc}", "error")
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
        flash("Contact moved successfully.", "success")
    except Exception as exc:
        app.logger.exception("Move failed")
        flash(f"Move failed: {exc}", "error")
    return redirect(url_for("profile_view_contact", profile_id=profile_id, collection_path=collection_path, contact_filename=contact_filename))


@app.route("/books/<path:collection_path>/contacts")
def view_contacts(collection_path):
    """Show all contacts stored in a selected Radicale address book."""
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
    """Show detail information for a single contact."""
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
    """Export the selected address book as a VCF download."""
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
        filename = f"radicale-{collection_path.replace('/', '_')}-{datetime.utcnow().date()}.vcf"
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
    """Edit a contact by loading it from Radicale and writing back updates."""
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
    """Delete a contact from a Radicale collection."""
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
    app.run(host="0.0.0.0", port=5000, debug=False)
