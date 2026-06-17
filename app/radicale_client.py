import logging
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
import requests
from requests.auth import HTTPBasicAuth

LOGGER = logging.getLogger(__name__)


class RadicaleClient:
    """Wrap Radicale HTTP interactions for address book and contact operations."""

    NAMESPACES = {"D": "DAV:"}
    PROP_BODY = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<D:propfind xmlns:D=\"DAV:\">
  <D:prop>
    <D:resourcetype/>
    <D:displayname/>
    <D:getetag/>
  </D:prop>
</D:propfind>"""

    def __init__(self, server_url, username, password):
        """Initialize the Radicale client using HTTP basic authentication."""
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.headers.update({"User-Agent": "radicale-uploader/1.0"})
        LOGGER.debug("Initialized RadicaleClient for %s", self.server_url)

    def _build_url(self, path):
        """Normalize paths into absolute URLs for Radicale endpoints."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        path = path.lstrip("/")
        return f"{self.server_url}/{path}"

    def propfind(self, path, depth=1, body=None):
        """Perform a WebDAV PROPFIND request and return the XML response tree."""
        url = self._build_url(path)
        if not url.endswith("/"):
            url += "/"
        headers = {"Depth": str(depth), "Content-Type": "application/xml"}
        LOGGER.debug("PROPFIND request URL=%s depth=%s", url, depth)
        response = self.session.request(
            "PROPFIND",
            url,
            headers=headers,
            data=body or self.PROP_BODY,
            timeout=30,
        )
        LOGGER.debug("PROPFIND response status=%s for URL=%s", response.status_code, url)
        response.raise_for_status()
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("PROPFIND response XML:\n%s", response.text)
        return ET.fromstring(response.text)

    def discover_addressbooks(self):
        """Discover address book collections available on the Radicale server."""
        root = self.propfind("", depth=1)
        books = []
        response_count = 0
        for resp in root.findall("D:response", self.NAMESPACES):
            response_count += 1
            href = resp.find("D:href", self.NAMESPACES)
            if href is None or not href.text:
                LOGGER.debug("Skipping response #%d without href", response_count)
                continue
            resourcetype = resp.find(".//D:resourcetype", self.NAMESPACES)
            if resourcetype is None:
                LOGGER.debug("Skipping response #%d without resourcetype", response_count)
                continue
            if resourcetype.find("D:addressbook", self.NAMESPACES) is None:
                LOGGER.debug("Skipping response #%d because no addressbook type was found", response_count)
                continue
            displayname = resp.find(".//D:displayname", self.NAMESPACES)
            if displayname is None or not displayname.text:
                LOGGER.debug("Skipping response #%d because displayname is missing", response_count)
                continue
            path = href.text.strip("/")
            if not path:
                LOGGER.debug("Skipping response #%d because href path is empty", response_count)
                continue
            books.append(
                {
                    "name": displayname.text,
                    "path": path,
                    "href": "/" + path + "/",
                }
            )
            LOGGER.debug("Found addressbook #%d path=%s displayname=%s", len(books), path, displayname.text)
        if not books:
            LOGGER.debug("Discovery parsed %d response entries but found no addressbooks", response_count)
        LOGGER.debug("Discovered %d addressbooks", len(books))
        return books

    def list_contacts(self, collection_path):
        """List all VCF contacts inside an address book collection."""
        collection_path = collection_path.strip("/")
        root = self.propfind(collection_path, depth=1)
        contacts = []
        response_count = 0
        for resp in root.findall("D:response", self.NAMESPACES):
            response_count += 1
            href = resp.find("D:href", self.NAMESPACES)
            if href is None or not href.text:
                LOGGER.debug("Skipping list contact response #%d without href", response_count)
                continue
            href_text = href.text.strip()
            if href_text.endswith("/"):
                LOGGER.debug("Skipping list contact response #%d because it is a directory: %s", response_count, href_text)
                continue
            if not href_text.lower().endswith(".vcf"):
                LOGGER.debug("Skipping list contact response #%d because it is not a VCF file: %s", response_count, href_text)
                continue
            contact_url = urljoin(self.server_url + "/", href_text.lstrip("/"))
            LOGGER.debug("Fetching contact from %s", contact_url)
            vcard_text, etag = self.get_contact(contact_url)
            contacts.append({"href": contact_url, "vcard": vcard_text, "etag": etag})
        LOGGER.debug("Processed %d contact responses and listed %d contacts in collection %s", response_count, len(contacts), collection_path)
        return contacts

    def get_contact(self, contact_href):
        """Fetch a single contact VCard and return its raw text and ETag."""
        url = self._build_url(contact_href)
        LOGGER.debug("Fetching contact %s", url)
        response = self.session.get(url, headers={"Accept": "text/vcard"}, timeout=30)
        response.raise_for_status()
        return response.text, response.headers.get("ETag")

    def put_contact(self, collection_path, filename, vcard_text, if_match=None):
        """Create or update a contact in the specified address book collection."""
        collection_path = collection_path.strip("/")
        target_url = self._build_url(f"{collection_path}/{filename}")
        headers = {"Content-Type": "text/vcard"}
        if if_match:
            headers["If-Match"] = if_match
        LOGGER.debug("Putting contact %s with ETag=%s", target_url, if_match)
        response = self.session.put(target_url, headers=headers, data=vcard_text.encode("utf-8"), timeout=30)
        response.raise_for_status()
        return response

    def delete_contact(self, contact_href):
        """Delete a contact from the Radicale server."""
        url = self._build_url(contact_href)
        LOGGER.debug("Deleting contact %s", url)
        response = self.session.delete(url, timeout=30)
        response.raise_for_status()
        return response

    def export_addressbook(self, collection_path):
        """Export all contacts in an address book as a single VCF string."""
        contacts = self.list_contacts(collection_path)
        exported = []
        for item in contacts:
            exported.append(item["vcard"].strip())
        LOGGER.debug("Exported %d contacts from address book %s", len(exported), collection_path)
        return "\n".join(exported) + "\n" if exported else ""
