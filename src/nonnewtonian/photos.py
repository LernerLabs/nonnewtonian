"""Photo fetching, validation, normalization, and content-hash storage.

Replaces the original pipeline's photo handling, whose audited failure
modes were: a shared ``test.jpg`` temp file, no timeout, no status
check (404 HTML pages saved as "images"), every photo re-downloaded on
every run, and hotlinked display URLs of which roughly half have rotted.

Here: a URL is fetched once, validated by magic bytes, re-encoded by
Pillow (which also defuses decompression bombs and exotic payloads), and
stored under a content-hash filename.  Display and slide generation only
ever touch the local file.

SSRF posture (per the plan's adversarial reviews): ``validate_url``
rejects non-http(s) schemes and any hostname resolving to a private,
loopback, link-local, or otherwise non-global address, and returns the
vetted IP; ``fetch_photo`` follows redirects manually, re-validates
every hop, PINS each connection to the vetted IP (``_pin_session_to_ip``,
via urllib3's ``_dns_host`` — connect to the IP, keep the hostname for
Host/SNI), and reads the body as a capped stream.  Pinning closes the
DNS-rebinding TOCTOU the M4 review found: a name that re-resolves to an
internal address at connect time can no longer be reached, because we
connect to the address we already vetted.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

# A generous ceiling for legitimate portraits; a hard error beyond it.
# Checked explicitly in normalize_image — deliberately NOT via
# Image.MAX_IMAGE_PIXELS / warnings.simplefilter, which are process
# globals any other code can reset (M1 adversarial review finding),
# and which a library should not mutate at import time anyway.
MAX_PIXELS = 30_000_000

MAX_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT = 5.0
MAX_REDIRECTS = 5

# The NAT64 well-known prefix passes ipaddress.is_global but can reach
# private IPv4 space through a NAT64 gateway.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

_MAGIC = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"RIFF": "webp",  # checked more precisely below
}


class PhotoError(ValueError):
    """A photo URL or payload was rejected; message says why, plainly."""


@dataclass
class StoredPhoto:
    path: Path
    original_url: str | None
    content_type: str
    width: int
    height: int
    sha256: str


def validate_url(url: str, *, resolver=socket.getaddrinfo) -> None:
    """Reject URLs this server must never fetch.  Raises PhotoError.

    Every rejection is a PhotoError with a teacher-readable message —
    including the paths where urllib.parse itself raises (invalid ports,
    malformed bracketed hosts; M1 review findings).

    Parser-differential note: urllib.parse and the HTTP client can split
    exotic authorities differently (e.g. a backslash or userinfo in the
    netloc), so a host validated here might not be the host connected
    to.  Defense: reject backslashes and userinfo outright, so the
    accepted subset parses identically everywhere.  Returns
    ``(hostname, port, vetted_ip)`` so ``fetch_photo`` can pin the
    connection to the address validated here.
    """
    if "\\" in url:
        raise PhotoError("That link contains a backslash, which is not valid in a web link.")
    try:
        parsed = urlparse(url)
    except ValueError:
        raise PhotoError("That doesn't look like a valid web link.") from None
    if parsed.scheme not in {"http", "https"}:
        raise PhotoError(f"Only http/https photo links work (got {parsed.scheme!r}).")
    if "@" in parsed.netloc:
        raise PhotoError("Links with an embedded username are not supported.")
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise PhotoError("That link has an invalid host or port.") from None
    if not hostname:
        raise PhotoError("That link has no host name.")
    try:
        infos = resolver(hostname, port or 0)
    except (socket.gaierror, UnicodeError):
        raise PhotoError(f"Could not look up {hostname!r}.") from None
    if not infos:
        raise PhotoError(f"Could not look up {hostname!r}.")
    vetted: list[str] = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise PhotoError(f"Could not look up {hostname!r}.") from None
        if not address.is_global or (address.version == 6 and address in _NAT64):
            raise PhotoError(
                "That link points at a private or internal address, "
                "which this site will not fetch."
            )
        vetted.append(str(address))
    # Return one vetted IP so the caller can PIN the connection to it,
    # closing the resolve-then-connect rebinding window (M4 review).
    return hostname, port, vetted[0]


def sniff_image_type(data: bytes) -> str:
    """Return an extension for known image magic bytes, else raise."""
    for magic, ext in _MAGIC.items():
        if data.startswith(magic):
            if ext == "webp" and data[8:12] != b"WEBP":
                continue
            return ext
    raise PhotoError(
        "That link did not return an image file (it may be an error page "
        "or a web page around the image; link directly to the image)."
    )


def normalize_image(data: bytes) -> tuple[bytes, str, int, int]:
    """Re-encode through Pillow: validates the file deeply, strips exotic
    payloads/metadata, converts to slide-safe JPEG (or PNG when there is
    transparency).  Returns (bytes, extension, width, height)."""
    sniff_image_type(data)
    try:
        with Image.open(io.BytesIO(data)) as image:
            # Header dimensions are available before decoding: reject
            # decompression bombs with a local, unconditional check that
            # no global warnings-filter state can disable.
            if image.width * image.height > MAX_PIXELS:
                raise PhotoError(
                    f"That image is implausibly large "
                    f"({image.width}x{image.height} pixels)."
                )
            image.load()
            has_alpha = image.mode in ("RGBA", "LA", "P") and (
                image.mode != "P" or "transparency" in image.info
            )
            buffer = io.BytesIO()
            if has_alpha:
                image.convert("RGBA").save(buffer, format="PNG")
                ext = "png"
            else:
                image.convert("RGB").save(buffer, format="JPEG", quality=90)
                ext = "jpg"
            return buffer.getvalue(), ext, image.width, image.height
    except PhotoError:
        raise
    except Image.DecompressionBombError:
        # Pillow's own (default, ~178M-pixel) ceiling tripped at open().
        raise PhotoError("That image is implausibly large.") from None
    except Exception as exc:  # Pillow raises many types on bad files
        raise PhotoError(f"Could not read that image file ({exc}).") from None


def store_bytes(data: bytes, dest_dir: Path, *, original_url: str | None = None) -> StoredPhoto:
    """Normalize and write image bytes under a content-hash filename."""
    normalized, ext, width, height = normalize_image(data)
    digest = hashlib.sha256(normalized).hexdigest()
    subdir = Path(dest_dir) / digest[:2]
    subdir.mkdir(parents=True, exist_ok=True)
    path = subdir / f"{digest}.{ext}"
    if not path.exists():
        path.write_bytes(normalized)
    return StoredPhoto(
        path=path,
        original_url=original_url,
        content_type=f"image/{'jpeg' if ext == 'jpg' else ext}",
        width=width,
        height=height,
        sha256=digest,
    )


def _pin_session_to_ip(session: requests.Session, host: str, ip: str) -> None:
    """Mount an adapter that dials the vetted IP for `host` while keeping
    the hostname for the Host header and TLS SNI/verification.  urllib3's
    ``_dns_host`` is the exact seam for this: the connection resolves and
    connects to ``_dns_host`` but presents ``host`` to TLS.  This closes
    the DNS-rebinding TOCTOU: even if the name re-resolves to a private
    address at connect time, we connect to the address we already vetted."""
    from urllib3 import PoolManager
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

    class _PinHTTP(HTTPConnection):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            if self.host == host:
                self._dns_host = ip

    class _PinHTTPS(HTTPSConnection):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            if self.host == host:
                self._dns_host = ip

    class _PinHTTPPool(HTTPConnectionPool):
        ConnectionCls = _PinHTTP

    class _PinHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = _PinHTTPS

    class _PinAdapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **kw):
            pm = PoolManager(num_pools=connections, maxsize=maxsize, block=block, **kw)
            pm.pool_classes_by_scheme = {"http": _PinHTTPPool, "https": _PinHTTPSPool}
            self.poolmanager = pm

    adapter = _PinAdapter(max_retries=0)
    session.mount(f"http://{host}", adapter)
    session.mount(f"https://{host}", adapter)


def fetch_photo(url: str, dest_dir: Path, *, session: requests.Session | None = None,
                resolver=socket.getaddrinfo) -> StoredPhoto:
    """Fetch, validate, normalize, and store one remote photo.

    Each hop is validated AND its connection pinned to the vetted IP, so
    a rebinding hostname cannot slip an internal address in between the
    resolve-check and the socket connect."""
    session = session or requests.Session()
    real_session = isinstance(session, requests.Session)  # tests inject fakes
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        host, _port, ip = validate_url(current, resolver=resolver)
        if real_session:
            _pin_session_to_ip(session, host, ip)
        response = session.get(
            current, stream=True, timeout=FETCH_TIMEOUT, allow_redirects=False
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            if not location:
                raise PhotoError("The link redirected without a destination.")
            current = requests.compat.urljoin(current, location)
            continue
        if response.status_code != 200:
            raise PhotoError(
                f"The link returned HTTP {response.status_code} instead of an image."
            )
        data = b""
        for chunk in response.iter_content(chunk_size=65536):
            data += chunk
            if len(data) > MAX_BYTES:
                raise PhotoError(
                    f"That image is larger than {MAX_BYTES // (1024 * 1024)} MB."
                )
        return store_bytes(data, dest_dir, original_url=url)
    raise PhotoError("Too many redirects.")


def extract_pptx_images(pptx_path) -> list[tuple[bytes, str]]:
    """Pull embedded images out of a .pptx (a zip) — the only surviving
    copies of photos whose source URLs have rotted.  Returns
    [(bytes, extension), ...] in archive order."""
    import zipfile

    images: list[tuple[bytes, str]] = []
    with zipfile.ZipFile(pptx_path) as archive:
        # Archive order, as documented — lexicographic sorting would put
        # image10 before image2.
        for name in archive.namelist():
            if name.startswith("ppt/media/"):
                ext = name.rsplit(".", 1)[-1].lower()
                if ext in {"jpg", "jpeg", "png", "gif", "webp"}:
                    images.append((archive.read(name), "jpg" if ext == "jpeg" else ext))
    return images
