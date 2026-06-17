import logging
import re
import uuid
import vobject

LOGGER = logging.getLogger(__name__)


def _split_vcards(content):
    """Split raw VCF content into individual VCARD blocks."""
    content = content.replace("\r\n", "\n").strip()
    pattern = re.compile(r"(?ms)^BEGIN:VCARD.*?END:VCARD(?:\n|$)")
    matches = pattern.findall(content)
    if matches:
        return [block.strip() + "\n" for block in matches]
    if content.upper().startswith("BEGIN:VCARD") and content.upper().strip().endswith("END:VCARD"):
        return [content + "\n"]
    return []


def parse_vcf_contacts(vcf_content):
    """Parse VCF content into a list of contacts and capture failed entries."""
    contacts = []
    failed = []
    blocks = _split_vcards(vcf_content)
    LOGGER.debug("Parsing VCF content into %d blocks", len(blocks))
    if not blocks:
        raise ValueError("Invalid VCF file or no contacts found.")

    for index, block in enumerate(blocks, start=1):
        try:
            card = vobject.readOne(block)
        except Exception as exc:
            LOGGER.warning("Failed to parse VCARD block #%d: %s", index, exc)
            failed.append({"error": str(exc), "vcard": block})
            continue

        if not hasattr(card, "uid") or not card.uid.value:
            card.add("uid").value = str(uuid.uuid4())
        if not hasattr(card, "version"):
            card.add("version").value = "3.0"

        if not hasattr(card, "fn"):
            if hasattr(card, "n"):
                full_name = _format_name(card.n.value)
                card.add("fn").value = full_name
            else:
                card.add("fn").value = card.uid.value

        vcard_text = card.serialize()
        filename = f"{card.uid.value}.vcf"
        display_name = card.fn.value if hasattr(card, "fn") else card.uid.value
        contacts.append(
            {
                "uid": card.uid.value,
                "filename": filename,
                "vcard": vcard_text,
                "display_name": display_name,
            }
        )
    LOGGER.info("Parsed %d contacts with %d failures", len(contacts), len(failed))
    return contacts, failed


def _format_name(name):
    """Convert a parsed name object into a printable full name."""
    if not name:
        return ""
    if hasattr(name, "given") and hasattr(name, "family"):
        return " ".join(part for part in [name.given, name.family] if part)
    return str(name)


def _get_property(card, prop_name):
    if hasattr(card, prop_name):
        return getattr(card, prop_name)
    if hasattr(card, "contents"):
        return card.contents.get(prop_name)
    return None


def _property_items(card, prop_name):
    prop = _get_property(card, prop_name)
    if prop is None:
        return []
    return prop if isinstance(prop, list) else [prop]


def _extract_text(prop):
    if prop is None:
        return ""
    try:
        value = getattr(prop, "value", prop)
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return " ".join(str(part).strip() for part in value if part)
        return str(value).strip()
    except Exception as exc:
        LOGGER.warning("Unexpected property format when extracting text: %s", exc)
        return ""


def _extract_list(card, prop_name):
    values = []
    for prop in _property_items(card, prop_name):
        text = _extract_text(prop)
        if text:
            values.append(text)
    return values


def extract_emails(card):
    """Return all email values from a parsed vCard object."""
    return _extract_list(card, "email")


def extract_phones(card):
    """Return all telephone values from a parsed vCard object."""
    return _extract_list(card, "tel")


def extract_address(card):
    """Return the formatted address value from a parsed vCard object."""
    addresses = []
    for adr_item in _property_items(card, "adr"):
        adr_value = getattr(adr_item, "value", None)
        if adr_value is None:
            continue
        if isinstance(adr_value, (list, tuple)):
            addresses.append(
                ", ".join(str(part).strip() for part in adr_value if part)
            )
        else:
            addresses.append(str(adr_value).strip())
    return "; ".join(address for address in addresses if address)


def extract_organization(card):
    """Return the organization string from a parsed vCard object."""
    org_values = _extract_list(card, "org")
    return org_values[0] if org_values else ""


def extract_note(card):
    """Return the note string from a parsed vCard object."""
    note_values = _extract_list(card, "note")
    return note_values[0] if note_values else ""


def _extract_name(card):
    n_items = _property_items(card, "n")
    if not n_items:
        return "", "", ""

    n_prop = n_items[0]
    name_value = getattr(n_prop, "value", n_prop)
    if name_value is None:
        return "", "", ""

    if hasattr(name_value, "given") or hasattr(name_value, "family"):
        first_name = getattr(name_value, "given", "") or ""
        last_name = getattr(name_value, "family", "") or ""
        full_name = " ".join(part for part in [first_name, last_name] if part)
        return full_name, first_name, last_name

    if isinstance(name_value, (list, tuple)):
        first_name = str(name_value[1]).strip() if len(name_value) > 1 else ""
        last_name = str(name_value[0]).strip() if len(name_value) > 0 else ""
        full_name = " ".join(part for part in [first_name, last_name] if part)
        return full_name, first_name, last_name

    name_text = str(name_value).strip()
    if name_text:
        parts = [part for part in name_text.split(";") if part]
        if len(parts) > 1:
            first_name = parts[1].strip()
            last_name = parts[0].strip()
            full_name = " ".join(part for part in [first_name, last_name] if part)
            return full_name, first_name, last_name
        return name_text, "", ""

    return "", "", ""


def vcard_to_dict(vcard_text):
    """Convert a raw VCard string to a dictionary of displayable fields."""
    item = {
        "uid": "",
        "full_name": "",
        "first_name": "",
        "last_name": "",
        "emails": [],
        "phones": [],
        "organization": "",
        "address": "",
        "note": "",
    }

    try:
        card = vobject.readOne(vcard_text)
    except Exception as exc:
        LOGGER.warning("Unable to parse vCard for vcard_to_dict: %s", exc)
        item["raw"] = vcard_text
        return item

    item["uid"] = _extract_text(_get_property(card, "uid"))
    item["full_name"] = _extract_text(_get_property(card, "fn"))

    # Prefer parsed N fields for first/last names and fallback full name when FN is missing.
    full_name, first_name, last_name = _extract_name(card)
    if not item["full_name"] and full_name:
        item["full_name"] = full_name
    item["first_name"] = first_name
    item["last_name"] = last_name

    item["emails"] = _extract_list(card, "email")
    item["phones"] = _extract_list(card, "tel")

    try:
        item["address"] = extract_address(card)
    except Exception as exc:
        LOGGER.warning("Unexpected ADR format: %s", exc)

    try:
        item["organization"] = extract_organization(card)
    except Exception as exc:
        LOGGER.warning("Unexpected ORG format: %s", exc)

    try:
        item["note"] = extract_note(card)
    except Exception as exc:
        LOGGER.warning("Unexpected NOTE format: %s", exc)

    item["raw"] = vcard_text
    return item


def build_vcard_from_fields(values):
    """Build a VCard string from form input values for editing or creating contacts."""
    card = vobject.vCard()
    card.add("version").value = "3.0"
    card.add("fn").value = values.get("full_name") or ""
    name = card.add("n")
    name.value = vobject.vcard.Name(
        family=values.get("last_name") or "",
        given=values.get("first_name") or "",
    )
    if values.get("organization"):
        org = card.add("org")
        org.value = [values.get("organization")]
    if values.get("note"):
        note = card.add("note")
        note.value = values.get("note")

    for email in values.get("emails", []):
        if email:
            email_item = card.add("email")
            email_item.value = email
            email_item.type_param = "INTERNET"

    for phone in values.get("phones", []):
        if phone:
            tel = card.add("tel")
            tel.value = phone
            tel.type_param = "VOICE"

    if values.get("address"):
        adr = card.add("adr")
        adr.value = ["", "", values.get("address"), "", "", "", ""]

    if values.get("uid"):
        uid = card.add("uid")
        uid.value = values["uid"]
    else:
        card.add("uid").value = str(uuid.uuid4())

    return card.serialize()


def get_contact_filename(contact_href):
    """Return the file name portion of a contact URL or href."""
    if not contact_href:
        return None
    return contact_href.rstrip("/").split("/")[-1]
