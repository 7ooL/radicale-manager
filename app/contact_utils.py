import base64
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
    if hasattr(card, "contents"):
        prop = card.contents.get(prop_name)
        if prop is not None:
            return prop
    if hasattr(card, prop_name):
        return getattr(card, prop_name)
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
        if isinstance(prop, (list, tuple)):
            return " ".join(_extract_text(item) for item in prop if _extract_text(item))
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
        prop_value = getattr(prop, "value", None)
        if isinstance(prop_value, (list, tuple)):
            values.extend(str(item).strip() for item in prop_value if item)
            continue
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
        addresses.append(_format_address_summary(_address_components(adr_value)))
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


def _normalize_property_parameters(params):
    normalized = {}
    if not params:
        return normalized
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            parts = []
            for item in value:
                if isinstance(item, (list, tuple)):
                    parts.extend(str(part) for part in item)
                else:
                    parts.append(str(item))
            normalized[key.upper()] = [part for part in parts if part]
        else:
            normalized[key.upper()] = [str(value)]
    return normalized


def _format_property_value(prop):
    if prop is None:
        return ""
    value = getattr(prop, "value", prop)
    if value is None:
        return ""
    if prop.name.upper() == "ADR":
        return _format_address_summary(_address_components(value))
    if prop.name.upper() == "N":
        if hasattr(value, "given") or hasattr(value, "family"):
            return " ".join(
                part for part in [getattr(value, "given", ""), getattr(value, "family", "")]
                if part
            )
        if isinstance(value, (list, tuple)):
            return " ".join(str(part).strip() for part in value if part)
    if isinstance(value, (list, tuple)):
        return ", ".join(str(part).strip() for part in value if part)
    return str(value).strip()


def _address_components(value):
    if value is None:
        return {}
    if hasattr(value, "street") or hasattr(value, "city") or hasattr(value, "region"):
        return {
            "po_box": getattr(value, "box", "") or "",
            "extended": getattr(value, "extended", "") or "",
            "street": getattr(value, "street", "") or "",
            "city": getattr(value, "city", "") or "",
            "region": getattr(value, "region", "") or "",
            "postal_code": getattr(value, "code", "") or "",
            "country": getattr(value, "country", "") or "",
        }
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() if item is not None else "" for item in value]
        while len(parts) < 7:
            parts.append("")
        return {
            "po_box": parts[0],
            "extended": parts[1],
            "street": parts[2],
            "city": parts[3],
            "region": parts[4],
            "postal_code": parts[5],
            "country": parts[6],
        }
    return {"formatted": str(value).strip()}


def _format_address_summary(address_components):
    if not address_components:
        return ""
    if "formatted" in address_components and address_components["formatted"]:
        return address_components["formatted"]
    parts = []
    if address_components.get("po_box"):
        parts.append(f"PO Box {address_components['po_box']}")
    if address_components.get("extended"):
        parts.append(address_components["extended"])
    if address_components.get("street"):
        parts.append(address_components["street"])
    city_region = ", ".join(
        part for part in [address_components.get("city"), address_components.get("region")] if part
    )
    if city_region:
        parts.append(city_region)
    if address_components.get("postal_code"):
        parts.append(address_components["postal_code"])
    if address_components.get("country"):
        parts.append(address_components["country"])
    return ", ".join(parts)


def _extract_addresses(card):
    addresses = []
    for adr_prop in _property_items(card, "adr"):
        adr_value = getattr(adr_prop, "value", None)
        components = _address_components(adr_value)
        addresses.append(
            {
                "label": _format_property_label(adr_prop.params) or "Address",
                "formatted": _format_address_summary(components),
                "components": components,
                "params": _normalize_property_parameters(adr_prop.params),
            }
        )
    return addresses


def _format_property_label(params):
    params = _normalize_property_parameters(params)
    if not params:
        return ""
    label_parts = []
    for name, values in params.items():
        label_parts.append(f"{name}={','.join(values)}")
    return ", ".join(label_parts)


def _extract_photo_summary(card):
    photo_props = _property_items(card, "photo")
    if not photo_props:
        return {}
    prop = photo_props[0]
    value = getattr(prop, "value", None)
    metadata = {}
    preview_url = None
    if isinstance(value, (bytes, bytearray)):
        content_type = None
        params = _normalize_property_parameters(prop.params)
        if params.get("TYPE"):
            type_value = params["TYPE"][0]
            if "/" in type_value:
                content_type = type_value
        if not content_type and params.get("MEDIATYPE"):
            content_type = params["MEDIATYPE"][0]
        if not content_type:
            content_type = "image/jpeg"
        encoded = base64.b64encode(value).decode("ascii")
        preview_url = f"data:{content_type};base64,{encoded}"
        metadata = {"size": len(value), "media_type": content_type}
    elif isinstance(value, str):
        if value.startswith("http://") or value.startswith("https://") or value.startswith("data:"):
            preview_url = value
            metadata = {"source": "uri"}
        else:
            metadata = {"value_preview": value[:80]}
    else:
        metadata = {"type": type(value).__name__, "value_preview": str(value)[:80]}
    return {"preview_url": preview_url, "metadata": metadata}


def parse_vcard_contact(vcard_text):
    summary = {
        "full_name": "",
        "nickname": "",
        "organization": "",
        "job_title": "",
        "birthday": "",
        "emails": [],
        "phones": [],
        "addresses": [],
        "urls": [],
        "notes": [],
        "categories": [],
        "photo": {},
    }
    advanced_fields = []
    additional_fields = []
    try:
        card = vobject.readOne(vcard_text)
    except Exception as exc:
        LOGGER.warning("Unable to parse vCard for parse_vcard_contact: %s", exc)
        return {"summary": summary, "advanced_fields": advanced_fields, "additional_fields": additional_fields, "raw": vcard_text}

    summary["full_name"] = _extract_text(_get_property(card, "fn")) or _extract_name(card)[0]
    summary["nickname"] = _extract_text(_get_property(card, "nickname"))
    summary["organization"] = extract_organization(card)
    summary["job_title"] = _extract_text(_get_property(card, "title"))
    summary["birthday"] = _extract_text(_get_property(card, "bday"))
    summary["emails"] = _extract_list(card, "email")
    summary["phones"] = _extract_list(card, "tel")
    summary["addresses"] = _extract_addresses(card)
    summary["urls"] = _extract_list(card, "url")
    summary["notes"] = _extract_list(card, "note")
    summary["categories"] = _extract_list(card, "categories")
    summary["photo"] = _extract_photo_summary(card)

    known_properties = {
        "FN",
        "N",
        "NICKNAME",
        "EMAIL",
        "TEL",
        "ADR",
        "ORG",
        "TITLE",
        "URL",
        "BDAY",
        "NOTE",
        "CATEGORIES",
        "UID",
        "PHOTO",
        "IMPP",
        "VERSION",
    }

    for child in getattr(card, "getChildren", lambda: [])():
        prop_name = child.name.upper()
        params = _normalize_property_parameters(getattr(child, "params", {}))
        display_value = _format_property_value(child)
        field_entry = {
            "property": prop_name,
            "params": params,
            "display_value": display_value,
            "raw_value": getattr(child, "value", None),
        }
        if prop_name == "ADR":
            field_entry["components"] = _address_components(getattr(child, "value", None))
        if prop_name == "PHOTO":
            field_entry["photo_preview"] = summary["photo"].get("preview_url")
            field_entry["photo_metadata"] = summary["photo"].get("metadata")
        advanced_fields.append(field_entry)
        if prop_name not in known_properties or prop_name.startswith("X-"):
            additional_fields.append(field_entry)

    return {
        "summary": summary,
        "advanced_fields": advanced_fields,
        "additional_fields": additional_fields,
        "raw": vcard_text,
    }


def vcard_to_dict(vcard_text):
    """Convert a raw VCard string to a dictionary of displayable fields."""
    item = {
        "uid": "",
        "full_name": "",
        "first_name": "",
        "last_name": "",
        "nickname": "",
        "emails": [],
        "phones": [],
        "organization": "",
        "job_title": "",
        "birthday": "",
        "address": "",
        "urls": [],
        "categories": [],
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
    item["nickname"] = _extract_text(_get_property(card, "nickname"))
    item["job_title"] = _extract_text(_get_property(card, "title"))
    item["birthday"] = _extract_text(_get_property(card, "bday"))

    # Prefer parsed N fields for first/last names and fallback full name when FN is missing.
    full_name, first_name, last_name = _extract_name(card)
    if not item["full_name"] and full_name:
        item["full_name"] = full_name
    item["first_name"] = first_name
    item["last_name"] = last_name

    item["emails"] = _extract_list(card, "email")
    item["phones"] = _extract_list(card, "tel")
    item["urls"] = _extract_list(card, "url")
    item["categories"] = _extract_list(card, "categories")

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
    if values.get("nickname"):
        card.add("nickname").value = values.get("nickname")
    if values.get("job_title"):
        card.add("title").value = values.get("job_title")
    if values.get("birthday"):
        card.add("bday").value = values.get("birthday")
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
        adr.value = vobject.vcard.Address(street=values.get("address"))

    for url in values.get("urls", []):
        if url:
            card.add("url").value = url

    if values.get("categories"):
        card.add("categories").value = values.get("categories")

    if values.get("uid"):
        uid = card.add("uid")
        uid.value = values["uid"]
    else:
        card.add("uid").value = str(uuid.uuid4())

    return card.serialize()


def merge_unknown_fields_into_vcard(original_vcard, rebuilt_vcard):
    """Merge non-standard/unknown properties from original into rebuilt vCard text."""
    known = {
        "BEGIN",
        "END",
        "VERSION",
        "FN",
        "N",
        "NICKNAME",
        "ORG",
        "TITLE",
        "EMAIL",
        "TEL",
        "ADR",
        "URL",
        "CATEGORIES",
        "NOTE",
        "BDAY",
        "UID",
        "PHOTO",
    }

    def unfold_lines(raw_text):
        lines = []
        for line in (raw_text or "").replace("\r\n", "\n").split("\n"):
            if not line:
                continue
            if line.startswith((" ", "\t")) and lines:
                lines[-1] += line[1:]
            else:
                lines.append(line)
        return lines

    unknown_lines = []
    for line in unfold_lines(original_vcard):
        if ":" not in line:
            continue
        prop = line.split(":", 1)[0].split(";", 1)[0].upper()
        if prop not in known:
            unknown_lines.append(line)

    if not unknown_lines:
        return rebuilt_vcard

    rebuilt = unfold_lines(rebuilt_vcard)
    end_index = next((idx for idx, line in enumerate(rebuilt) if line.upper() == "END:VCARD"), len(rebuilt))
    merged = rebuilt[:end_index] + unknown_lines + rebuilt[end_index:]
    return "\n".join(merged) + "\n"


def apply_typed_contact_methods_to_vcard(vcard_text, typed_emails=None, typed_phones=None):
    """Replace EMAIL/TEL fields with typed values, preserving TYPE params when provided."""
    typed_emails = typed_emails or []
    typed_phones = typed_phones or []
    if not typed_emails and not typed_phones:
        return vcard_text
    try:
        card = vobject.readOne(vcard_text)
    except Exception as exc:
        LOGGER.warning("Unable to parse vCard for typed method merge: %s", exc)
        return vcard_text

    if hasattr(card, "contents"):
        card.contents.pop("email", None)
        card.contents.pop("tel", None)
    for attr_name in ("email", "tel"):
        if hasattr(card, attr_name):
            try:
                delattr(card, attr_name)
            except Exception:
                pass

    for item in typed_emails:
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        email_prop = card.add("email")
        email_prop.value = value
        type_values = [str(v).strip().upper() for v in (item.get("types") or []) if str(v).strip()]
        if type_values:
            email_prop.params["TYPE"] = type_values
        else:
            email_prop.type_param = "INTERNET"

    for item in typed_phones:
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        phone_prop = card.add("tel")
        phone_prop.value = value
        type_values = [str(v).strip().upper() for v in (item.get("types") or []) if str(v).strip()]
        if type_values:
            phone_prop.params["TYPE"] = type_values
        else:
            phone_prop.type_param = "VOICE"

    return card.serialize()


def duplicate_vcard(vcard_text):
    """Return a copied vCard with a new UID and a display name marked as a copy."""
    card = vobject.readOne(vcard_text)
    new_uid = str(uuid.uuid4())
    if hasattr(card, "uid"):
        card.uid.value = new_uid
    else:
        card.add("uid").value = new_uid

    if hasattr(card, "fn") and card.fn.value:
        card.fn.value = f"{card.fn.value} Copy"
    else:
        card.add("fn").value = f"{new_uid} Copy"

    return new_uid, card.serialize()


def get_contact_filename(contact_href):
    """Return the file name portion of a contact URL or href."""
    if not contact_href:
        return None
    return contact_href.rstrip("/").split("/")[-1]
