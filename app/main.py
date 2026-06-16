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
)
from radicale_client import RadicaleClient
from contact_utils import parse_vcf_contacts, vcard_to_dict, build_vcard_from_fields, get_contact_filename
from credential_store import CredentialStore


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
    return app


app = create_app()
credential_store = CredentialStore(app.config["PROFILE_STORE_PATH"], app.config["PROFILE_SECRET"])


def get_active_connection():
    return session.get("active_connection")


def set_active_connection(server_url, username, password):
    session["active_connection"] = {
        "server_url": server_url,
        "username": username,
        "password": password,
    }


def clear_active_connection():
    session.pop("active_connection", None)


def make_client():
    active = get_active_connection()
    if not active:
        return None
    return RadicaleClient(active["server_url"], active["username"], active["password"])


@app.route("/", methods=["GET", "POST"])
def login():
    profiles = credential_store.get_profiles()
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
            profile = credential_store.get_profile(selected_profile_id)
            if profile:
                form["server_url"] = profile["server_url"]
                form["username"] = profile["username"]
                form["password"] = profile.get("password") or ""
                form["profile_name"] = profile["name"]
            return render_template(
                "login.html",
                title="Login",
                profiles=profiles,
                form=form,
                selected_profile_id=selected_profile_id,
            )

        form["server_url"] = request.form.get("server_url", app.config["DEFAULT_RADICALE_URL"]).strip()
        form["username"] = request.form.get("username", "").strip()
        form["password"] = request.form.get("password", "")
        form["profile_name"] = request.form.get("profile_name", "").strip()
        form["save_profile"] = bool(request.form.get("save_profile"))

        if not form["server_url"] or not form["username"] or not form["password"]:
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
            if not books:
                flash("No address books found on the server.", "error")
                return render_template("login.html", title="Login", profiles=profiles, form=form)

            set_active_connection(form["server_url"], form["username"], form["password"])
            if form["save_profile"] and form["profile_name"]:
                credential_store.save_profile(
                    form["profile_name"],
                    form["server_url"],
                    form["username"],
                    form["password"],
                    save_password=True,
                )

            return redirect(url_for("dashboard"))
        except Exception as exc:
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
    clear_active_connection()
    flash("Disconnected from Radicale.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    client = make_client()
    if not client:
        flash("Please connect first.", "error")
        return redirect(url_for("login"))

    try:
        books = client.discover_addressbooks()
        enriched = []
        for book in books:
            try:
                contacts = client.list_contacts(book["path"])
                enriched.append({**book, "count": len(contacts)})
            except Exception:
                enriched.append({**book, "count": None})
        return render_template("dashboard.html", title="Dashboard", books=enriched)
    except Exception as exc:
        flash(f"Unable to retrieve address books: {exc}", "error")
        return redirect(url_for("login"))


@app.route("/import", methods=["GET", "POST"])
def import_vcf():
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
        if not selected_path or not file:
            flash("Please select an address book and upload a VCF file.", "error")
            return render_template("import.html", title="Import VCF", books=books, selected_path=selected_path)

        try:
            content = file.read().decode("utf-8", errors="replace")
            contacts, failed = parse_vcf_contacts(content)
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
                except Exception:
                    failed.append({"error": f"Failed to upload {contact['filename']}"})
            results = {"processed": processed, "created": created, "updated": updated, "failed": failed}
            flash("Import completed.", "success")
        except Exception as exc:
            flash(f"Import failed: {exc}", "error")

    return render_template(
        "import.html",
        title="Import VCF",
        books=books,
        selected_path=selected_path,
        results=results,
    )


@app.route("/books/<path:collection_path>/contacts")
def view_contacts(collection_path):
    client = make_client()
    if not client:
        flash("Please connect first.", "error")
        return redirect(url_for("login"))

    try:
        books = client.discover_addressbooks()
        book = next((b for b in books if b["path"] == collection_path), None)
        if not book:
            flash("Address book not found.", "error")
            return redirect(url_for("dashboard"))
        contacts = []
        for item in client.list_contacts(collection_path):
            parsed = vcard_to_dict(item["vcard"])
            parsed["filename"] = get_contact_filename(item["href"])
            contacts.append(parsed)
        return render_template(
            "contacts.html",
            title=f"Contacts - {book['name']}",
            book=book,
            contacts=contacts,
        )
    except Exception as exc:
        flash(f"Unable to load contacts: {exc}", "error")
        return redirect(url_for("dashboard"))


@app.route("/books/<path:collection_path>/export")
def export_addressbook(collection_path):
    client = make_client()
    if not client:
        flash("Please connect first.", "error")
        return redirect(url_for("login"))

    try:
        content = client.export_addressbook(collection_path)
        if not content:
            flash("No contacts available to export.", "error")
            return redirect(url_for("view_contacts", collection_path=collection_path))
        filename = f"radicale-{collection_path.replace('/', '_')}-{datetime.utcnow().date()}.vcf"
        buffer = BytesIO(content.encode("utf-8"))
        return send_file(
            buffer,
            download_name=filename,
            mimetype="text/vcard",
            as_attachment=True,
        )
    except Exception as exc:
        flash(f"Export failed: {exc}", "error")
        return redirect(url_for("view_contacts", collection_path=collection_path))


@app.route("/books/<path:collection_path>/contacts/<contact_filename>/edit", methods=["GET", "POST"])
def edit_contact(collection_path, contact_filename):
    client = make_client()
    if not client:
        flash("Please connect first.", "error")
        return redirect(url_for("login"))

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
            return redirect(url_for("view_contacts", collection_path=collection_path))
        return render_template(
            "edit_contact.html",
            title="Edit Contact",
            book={"path": collection_path, "name": collection_path},
            contact={"filename": contact_filename},
            fields=fields,
        )
    except Exception as exc:
        flash(f"Unable to edit contact: {exc}", "error")
        return redirect(url_for("view_contacts", collection_path=collection_path))


@app.route("/books/<path:collection_path>/contacts/<contact_filename>/delete")
def delete_contact(collection_path, contact_filename):
    client = make_client()
    if not client:
        flash("Please connect first.", "error")
        return redirect(url_for("login"))

    contact_href = f"{collection_path.rstrip('/')}/{contact_filename}"
    try:
        client.delete_contact(contact_href)
        flash("Contact deleted.", "success")
    except Exception as exc:
        flash(f"Delete failed: {exc}", "error")
    return redirect(url_for("view_contacts", collection_path=collection_path))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
