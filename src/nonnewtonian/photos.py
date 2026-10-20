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
import http.client
import io
import ipaddress
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
import urllib3
from PIL import Image

# A generous ceiling for legitimate portraits; a hard error beyond it.
# Checked explicitly in normalize_image — deliberately NOT via
# Image.MAX_IMAGE_PIXELS / warnings.simplefilter, which are process
# globals any other code can reset (M1 adversarial review finding),
# and which a library should not mutate at import time anyway.
MAX_PIXELS = 30_000_000
SLIDE_LONG_EDGE = 2000   # stored photos are shrunk to this; slides are ~1920 px wide
MAX_PIXELS_FULL_DECODE = 12_000_000  # non-JPEG: no reduced decode (see normalize_image)

# One normalization at a time: each can still peak near 130 MB (see
# normalize_image).  Correct only because gunicorn runs ONE process.
# Separate from the web layer's deck slot, so one normalization can
# overlap one deck build; the deck side is bounded by its photo-bytes
# cap (views_public.DECK_PHOTO_BYTES_CAP).  A submit that waits longer
# than this keeps its entry and loses only the photo, with a warning.
_NORMALIZE_SLOT = threading.BoundedSemaphore(1)
NORMALIZE_WAIT_SECONDS = 2  # a waiter holds a worker thread; fail fast like the deck slot

MAX_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT = 5.0      # per connect/read
FETCH_DEADLINE = 20.0    # whole fetch incl. redirects; gthread's timeout is a heartbeat, not a per-request limit
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
    payloads/metadata, shrinks to SLIDE_LONG_EDGE, converts to slide-safe
    JPEG (or PNG when there is transparency).  Returns (bytes, extension,
    width, height) of the stored image.

    Memory (M7 review #2): Pillow holds 4 bytes/pixel, so a 30 MP image
    decodes to about 120 MB, and the old full-size convert() copy made
    one call peak near 231 MB (measured) against MemoryMax=400M.  Now:
    JPEGs decode at a reduced DCT scale via draft() (the full raster
    never exists) and everything is shrunk by thumbnail() before
    convert(); other formats have no reduced decode, so they are capped
    at MAX_PIXELS_FULL_DECODE; and one slot serializes normalizations.
    Peak RSS growth measured on macOS (Linux numbers only on the server):
    30 MP JPEG 231 -> 63 MB; 12 MP PNG 82 MB (RGB) / 128 MB (RGBA, whose
    resize makes a premultiplied full-size copy)."""
    sniff_image_type(data)
    if not _NORMALIZE_SLOT.acquire(timeout=NORMALIZE_WAIT_SECONDS):
        # Worded to sit inside submit_thanks.html's "Your photo didn't
        # upload (...), but everything else was saved." sentence.
        raise PhotoError("the server was busy with other photos just now")
    try:
        return _normalize(data)
    finally:
        _NORMALIZE_SLOT.release()


def _normalize(data: bytes) -> tuple[bytes, str, int, int]:
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
            has_alpha = image.mode in ("RGBA", "LA", "P") and (
                image.mode != "P" or "transparency" in image.info
            )
            scale = SLIDE_LONG_EDGE / max(image.size)
            if image.format == "JPEG":
                if scale < 1:
                    # Decode at a reduced DCT scale (1/2 .. 1/8), never
                    # below the slide size: the full raster never exists.
                    image.draft(None, (max(1, int(image.width * scale)),
                                       max(1, int(image.height * scale))))
            elif image.width * image.height > MAX_PIXELS_FULL_DECODE:
                raise PhotoError(
                    f"That image is too large for this site "
                    f"({image.width}x{image.height} pixels); please use a JPEG "
                    f"or an image under {MAX_PIXELS_FULL_DECODE // 1_000_000} megapixels."
                )
            # Loads the pixels (the deep validation) and shrinks in place.
            image.thumbnail((SLIDE_LONG_EDGE, SLIDE_LONG_EDGE))
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
    # A zero-length file under the final name (left by a host crash before
    # this fsync existed) is treated as missing, so re-uploading repairs it.
    if not path.exists() or path.stat().st_size == 0:
        # Write-then-rename so a crash or ENOSPC never leaves a truncated
        # file under the final content-hash name (which would then be
        # trusted forever by the exists() check above).  fsync the data
        # before the rename and the directory after it: on ext4 a rename
        # to a new name can reach the journal before the data does, so a
        # hard reboot could otherwise leave a 0-byte file (M7 review #18).
        # The tmp name is per process and thread so concurrent stores of
        # one image cannot clobber each other's partial writes.
        # Known gap: a SIGKILL (e.g. OOM) mid-write skips the cleanup below
        # and leaves a ".<digest>.<pid>.<tid>.tmp" orphan; nothing sweeps
        # them (harmless to serving, but restic backs them up).
        tmp = subdir / f".{digest}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "wb") as handle:
                handle.write(normalized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            dir_fd = os.open(subdir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return StoredPhoto(
        path=path,
        original_url=original_url,
        content_type=f"image/{'jpeg' if ext == 'jpg' else ext}",
        width=width,
        height=height,
        sha256=digest,
    )


def _pin_session_to_ip(session: requests.Session, host: str, ip: str,
                       on_connect=None) -> None:
    """Mount an adapter that dials the vetted IP for `host` while keeping
    the hostname for the Host header and TLS SNI/verification.  urllib3's
    ``_dns_host`` is the exact seam for this: the connection resolves and
    connects to ``_dns_host`` but presents ``host`` to TLS.  This closes
    the DNS-rebinding TOCTOU: even if the name re-resolves to a private
    address at connect time, we connect to the address we already vetted.

    ``on_connect(sock)`` is called with each raw TCP socket as soon as it
    is connected (before TLS, before the request is sent), so
    ``fetch_photo``'s deadline watchdog can shut it down."""
    from urllib3 import PoolManager
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

    class _Pin:
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            if self.host == host:
                self._dns_host = ip

        def _new_conn(self):
            sock = super()._new_conn()
            if on_connect is not None:
                on_connect(sock)
            return sock

    class _PinHTTP(_Pin, HTTPConnection):
        pass

    class _PinHTTPS(_Pin, HTTPSConnection):
        pass

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


class _Deadline:
    """A total wall-clock deadline that interrupts blocked socket reads.

    FETCH_TIMEOUT bounds each connect/recv, but it restarts on every
    byte: a server that trickles a byte every few seconds never trips
    it, and urllib3 2.x's ``stream(amt)`` blocks until it has ``amt``
    bytes, so a check between chunks is never reached either (M7 review
    #1).  Instead a timer thread shuts down every socket this fetch
    opened when the deadline passes; the blocked recv (status line,
    headers, or body) then returns at once and the fetch fails as "too
    slow".  shutdown(), not close(): close() from another thread does
    not wake a recv blocked on that socket.

    Not covered: the getaddrinfo() lookups in validate_url, which Python
    cannot interrupt; they are bounded by the system resolver's own
    timeout and attempts (resolv.conf), not by FETCH_DEADLINE."""

    def __init__(self, seconds: float):
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self.expired = threading.Event()
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    @staticmethod
    def _shut(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:  # already closed (fd is -1 then, so no fd-reuse hazard)
            pass

    def register(self, sock):
        with self._lock:
            self._sockets.append(sock)
            if self.expired.is_set():
                self._shut(sock)

    def _expire(self):
        with self._lock:
            self.expired.set()
            for sock in self._sockets:
                self._shut(sock)

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.cancel()
        return False


def fetch_photo(url: str, dest_dir: Path, *, session: requests.Session | None = None,
                resolver=socket.getaddrinfo) -> StoredPhoto:
    """Fetch, validate, normalize, and store one remote photo.

    Each hop is validated AND its connection pinned to the vetted IP, so
    a rebinding hostname cannot slip an internal address in between the
    resolve-check and the socket connect."""
    session = session or requests.Session()
    real_session = isinstance(session, requests.Session)  # tests inject fakes
    start = time.monotonic()
    too_slow = "That site was too slow to send the image."

    with _Deadline(FETCH_DEADLINE) as deadline:

        def _check():
            # The watchdog does the real work for real sockets.  This also
            # covers injected sessions, and a body that "ended" early
            # because the watchdog shut the socket (no Content-Length).
            if deadline.expired.is_set() or time.monotonic() - start > FETCH_DEADLINE:
                raise PhotoError(too_slow)

        try:
            data = _download(url, session, real_session, resolver, deadline, _check)
        except (OSError, urllib3.exceptions.HTTPError, http.client.HTTPException) as exc:
            # Every requests exception is an OSError subclass; the urllib3
            # and http.client bases cover paths requests does not wrap.
            # Unwrapped, views_class reported an unreachable link as "the
            # server could not store the file" (M7 review #16).
            if deadline.expired.is_set():
                raise PhotoError(too_slow) from None
            raise PhotoError(
                "Could not download that link; check that it opens in a browser."
            ) from exc
        finally:
            if real_session:
                session.close()
        _check()
    return store_bytes(data, dest_dir, original_url=url)


def _download(url, session, real_session, resolver, deadline, _check) -> bytes:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        host, _port, ip = validate_url(current, resolver=resolver)
        if real_session:
            _pin_session_to_ip(session, host, ip, on_connect=deadline.register)
        response = session.get(
            current, stream=True, timeout=FETCH_TIMEOUT, allow_redirects=False
        )
        _check()
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
            _check()
            if len(data) > MAX_BYTES:
                raise PhotoError(
                    f"That image is larger than {MAX_BYTES // (1024 * 1024)} MB."
                )
        return data
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
