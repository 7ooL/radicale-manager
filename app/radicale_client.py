import xml.etree.ElementTree as ET
from urllib.parse import urljoin
import requests
from requests.auth import HTTPBasicAuth


class RadicaleClient:
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
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.headers.update({"User-Agent": "radicale-uploader/1.0"})

    def _build_url(self, path):
        if path.startswith("http://") or path.startswith("https://"):
            return path
        path = path.lstrip("/")
        return f"{self.server_url}/{path}"

    def propfind(self, path, depth=1, body=None):
        url = self._build_url(path)
        if not url.endswith("/"):
            url += "/"
        headers = {"Depth": str(depth), "Content-Type": "application/xml"}
        response = self.session.request(
            "PROPFIND",
            url,
            headers=headers,
            data=body or self.PROP_BODY,
            timeout=30,
        )
        response.raise_for_status()
        return ET.fromstring(response.text)

    def discover_addressbooks(self):
        root = self.propfind("", depth=1)
        books = []
        for resp in root.findall("D:response", self.NAMESPACES):
            href = resp.find("D:href", self.NAMESPACES)
            if href is None or not href.text:
                continue
            resourcetype = resp.find(".//D:resourcetype", self.NAMESPACES)
            if resourcetype is None:
                continue
            if resourcetype.find("D:addressbook", self.NAMESPACES) is None:
                continue
            displayname = resp.find(".//D:displayname", self.NAMESPACES)
            if displayname is None or not displayname.text:
                continue
            path = href.text.strip("/")
            if not path:
                continue
            books.append(
                {
                    "name": displayname.text,
                    "path": path,
                    "href": "/" + path + "/",
                }
            )
        return books

    def list_contacts(self, collection_path):
        collection_path = collection_path.strip("/")
        root = self.propfind(collection_path, depth=1)
        contacts = []
        for resp in root.findall("D:response", self.NAMESPACES):
            href = resp.find("D:href", self.NAMESPACES)
            if href is None or not href.text:
                continue
            href_text = href.text.strip()
            if href_text.endswith("/"):
                continue
            if not href_text.lower().endswith(".vcf"):
                continue
            contact_url = urljoin(self.server_url + "/", href_text.lstrip("/"))
            vcard_text, etag = self.get_contact(contact_url)
            contacts.append({"href": contact_url, "vcard": vcard_text, "etag": etag})
        return contacts

    def get_contact(self, contact_href):
        url = self._build_url(contact_href)
        response = self.session.get(url, headers={"Accept": "text/vcard"}, timeout=30)
        response.raise_for_status()
        return response.text, response.headers.get("ETag")

    def put_contact(self, collection_path, filename, vcard_text, if_match=None):
        collection_path = collection_path.strip("/")
        target_url = self._build_url(f"{collection_path}/{filename}")
        headers = {"Content-Type": "text/vcard"}
        if if_match:
            headers["If-Match"] = if_match
        response = self.session.put(target_url, headers=headers, data=vcard_text.encode("utf-8"), timeout=30)
        response.raise_for_status()
        return response

    def delete_contact(self, contact_href):
        url = self._build_url(contact_href)
        response = self.session.delete(url, timeout=30)
        response.raise_for_status()
        return response

    def export_addressbook(self, collection_path):
        contacts = self.list_contacts(collection_path)
        exported = []
        for item in contacts:
            exported.append(item["vcard"].strip())
        return "\n".join(exported) + "\n" if exported else ""
