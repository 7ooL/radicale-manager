import logging
import re
import uuid
import vobject
from vobject.base import ParseError

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

    for block in blocks:
        try:
            card = vobject.readOne(block)
        except Exception as exc:
            LOGGER.warning("Failed to parse VCARD block: %s", exc)
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


def vcard_to_dict(vcard_text):
    """Convert a raw VCard string to a dictionary of displayable fields."""
    card = vobject.readOne(vcard_text)
    item = {
        "uid": getattr(card, "uid", None).value if hasattr(card, "uid") else "",
        "full_name": getattr(card, "fn", None).value if hasattr(card, "fn") else "",
        "first_name": getattr(card, "n", None).value.given if hasattr(card, "n") and getattr(card.n.value, "given", None) else "",
        "last_name": getattr(card, "n", None).value.family if hasattr(card, "n") and getattr(card.n.value, "family", None) else "",
        "organization": getattr(card, "org", None).value[0] if hasattr(card, "org") and card.org.value else "",
        "note": getattr(card, "note", None).value if hasattr(card, "note") else "",
        "emails": [],
        "phones": [],
        "address": "",
        "raw": vcard_text,
    }

    if hasattr(card, "email"):
        emails = card.email if isinstance(card.email, list) else [card.email]
        item["emails"] = [email.value for email in emails if getattr(email, "value", None)]

    if hasattr(card, "tel"):
        phones = card.tel if isinstance(card.tel, list) else [card.tel]
        item["phones"] = [tel.value for tel in phones if getattr(tel, "value", None)]

    if hasattr(card, "adr"):
        adr_values = card.adr.value
        item["address"] = " ".join(part for part in adr_values if part)

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
