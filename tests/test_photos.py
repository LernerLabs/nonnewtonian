"""Photo pipeline: URL validation (SSRF posture), magic-byte sniffing,
Pillow normalization, content-hash storage, and pptx image recovery."""

import io

import pytest
from PIL import Image

from nonnewtonian.photos import (
    PhotoError,
    extract_pptx_images,
    normalize_image,
    sniff_image_type,
    store_bytes,
    validate_url,
)

from conftest import FIXTURES


def _fake_resolver(mapping):
    def resolver(host, port):
        if host not in mapping:
            import socket

            raise socket.gaierror(host)
        return [(2, 1, 6, "", (mapping[host], port or 80))]

    return resolver


def _jpeg_bytes(width=40, height=60, color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


class TestValidateUrl:
    def test_rejects_non_http_schemes(self):
        for url in ["file:///etc/passwd", "gopher://x/", "ftp://x/a.jpg"]:
            with pytest.raises(PhotoError, match="http"):
                validate_url(url, resolver=_fake_resolver({}))

    def test_rejects_private_loopback_linklocal_metadata(self):
        cases = {
            "internal.example": "10.1.2.3",
            "loop.example": "127.0.0.1",
            "metadata.example": "169.254.169.254",
            "lan.example": "192.168.1.10",
        }
        for host, ip in cases.items():
            with pytest.raises(PhotoError, match="private or internal"):
                validate_url(
                    f"http://{host}/x.jpg", resolver=_fake_resolver({host: ip})
                )

    def test_accepts_global_addresses(self):
        validate_url(
            "https://photos.example/x.jpg",
            resolver=_fake_resolver({"photos.example": "93.184.216.34"}),
        )

    def test_rejects_unresolvable_host(self):
        with pytest.raises(PhotoError, match="look up"):
            validate_url("https://nope.example/x.jpg", resolver=_fake_resolver({}))

    def test_invalid_ports_raise_photoerror_not_valueerror(self):
        """M1 review: urlparse's .port raises bare ValueError — in the
        web app that's a 500 on a teacher-pasted URL."""
        for url in ["http://example.com:99999/x.jpg", "http://example.com:0x50/x.jpg"]:
            with pytest.raises(PhotoError, match="invalid host or port"):
                validate_url(url, resolver=_fake_resolver({}))

    def test_malformed_bracketed_host_raises_photoerror(self):
        """urlparse itself can raise before our checks run."""
        with pytest.raises(PhotoError):
            validate_url(
                "http://[::1]\\@public.example/", resolver=_fake_resolver({})
            )

    def test_backslash_and_userinfo_rejected(self):
        """Parser-differential SSRF defense: urllib.parse and the HTTP
        client can disagree about exotic authorities, so the accepted
        subset excludes them entirely."""
        with pytest.raises(PhotoError, match="backslash"):
            validate_url("http://good.example\\@evil.example/x.jpg",
                         resolver=_fake_resolver({}))
        with pytest.raises(PhotoError, match="username"):
            validate_url("http://user@host.example/x.jpg",
                         resolver=_fake_resolver({"host.example": "93.184.216.34"}))

    def test_nat64_prefix_rejected(self):
        """64:ff9b::/96 is is_global but reaches private IPv4 through a
        NAT64 gateway."""
        with pytest.raises(PhotoError, match="private or internal"):
            validate_url(
                "http://nat64.example/x.jpg",
                resolver=_fake_resolver({"nat64.example": "64:ff9b::a9fe:a9fe"}),
            )


class TestImageHandling:
    def test_sniff_rejects_html_error_pages(self):
        """The original pipeline saved 404 HTML pages as test.jpg."""
        with pytest.raises(PhotoError, match="not return an image"):
            sniff_image_type(b"<!DOCTYPE html><html>404</html>")

    def test_normalize_reencodes_jpeg(self):
        data, ext, width, height = normalize_image(_jpeg_bytes())
        assert ext == "jpg" and (width, height) == (40, 60)
        Image.open(io.BytesIO(data))  # round-trips through Pillow

    def test_normalize_converts_transparent_png_to_png(self):
        buffer = io.BytesIO()
        Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(buffer, format="PNG")
        _, ext, _, _ = normalize_image(buffer.getvalue())
        assert ext == "png"

    @pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
    def test_decompression_bomb_rejected(self):
        """A tiny file that decodes to an enormous bitmap.  The check is
        local (header dimensions vs MAX_PIXELS) — a process-global
        warnings filter that other code could reset is not involved
        (M1 review finding), so no global state is touched here either."""
        buffer = io.BytesIO()
        Image.new("1", (12000, 12000)).save(buffer, format="PNG")  # 144 Mpx > 30 Mpx cap
        with pytest.raises(PhotoError, match="implausibly large"):
            normalize_image(buffer.getvalue())

    def test_store_bytes_content_hash_layout(self, tmp_path):
        stored = store_bytes(_jpeg_bytes(), tmp_path, original_url="http://x/a.jpg")
        assert stored.path.exists()
        assert stored.path.parent.name == stored.sha256[:2]
        assert stored.path.name == f"{stored.sha256}.jpg"
        # Idempotent: same bytes, same path, no duplicate files.
        again = store_bytes(_jpeg_bytes(), tmp_path)
        assert again.path == stored.path
        assert sum(1 for _ in tmp_path.rglob("*.jpg")) == 1


class _FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.is_redirect = status in (301, 302, 303, 307, 308) and "Location" in self.headers
        self.is_permanent_redirect = status in (301, 308) and "Location" in self.headers

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


class _FakeSession:
    """Scripted responses keyed by URL; records what was requested."""

    def __init__(self, responses):
        self.responses = responses
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.responses[url]


class TestFetchPhoto:
    """fetch_photo had zero coverage: a mutation run showed the status
    check, size cap, and per-hop revalidation could all be deleted with
    the suite green (M1 review finding)."""

    RESOLVER = staticmethod(
        _fake_resolver(
            {"pub.example": "93.184.216.34", "evil.example": "10.0.0.5"}
        )
    )

    def _fetch(self, session, url, tmp_path):
        from nonnewtonian.photos import fetch_photo

        return fetch_photo(url, tmp_path, session=session, resolver=self.RESOLVER)

    def test_success_stores_normalized_image(self, tmp_path):
        session = _FakeSession(
            {"https://pub.example/a.jpg": _FakeResponse(body=_jpeg_bytes())}
        )
        stored = self._fetch(session, "https://pub.example/a.jpg", tmp_path)
        assert stored.path.exists()
        assert stored.original_url == "https://pub.example/a.jpg"

    def test_non_200_raises_with_status_in_message(self, tmp_path):
        session = _FakeSession({"https://pub.example/a.jpg": _FakeResponse(status=404)})
        with pytest.raises(PhotoError, match="404"):
            self._fetch(session, "https://pub.example/a.jpg", tmp_path)

    def test_oversize_body_rejected(self, tmp_path):
        from nonnewtonian.photos import MAX_BYTES

        big = b"\xff\xd8\xff" + b"\x00" * (MAX_BYTES + 1)
        session = _FakeSession({"https://pub.example/a.jpg": _FakeResponse(body=big)})
        with pytest.raises(PhotoError, match="larger than"):
            self._fetch(session, "https://pub.example/a.jpg", tmp_path)

    def test_redirect_to_private_host_rejected(self, tmp_path):
        """Per-hop revalidation: hop 1 is public, hop 2 resolves private."""
        session = _FakeSession(
            {
                "https://pub.example/a.jpg": _FakeResponse(
                    status=302, headers={"Location": "https://evil.example/b.jpg"}
                ),
                "https://evil.example/b.jpg": _FakeResponse(body=_jpeg_bytes()),
            }
        )
        with pytest.raises(PhotoError, match="private or internal"):
            self._fetch(session, "https://pub.example/a.jpg", tmp_path)
        # It never actually requested the private host.
        assert session.requested == ["https://pub.example/a.jpg"]

    def test_redirect_loop_bounded(self, tmp_path):
        session = _FakeSession(
            {
                "https://pub.example/a.jpg": _FakeResponse(
                    status=302, headers={"Location": "https://pub.example/a.jpg"}
                )
            }
        )
        with pytest.raises(PhotoError, match="redirects"):
            self._fetch(session, "https://pub.example/a.jpg", tmp_path)


class TestPptxRecovery:
    def test_extracts_images_from_real_decks(self):
        """The existing decks are the only surviving copies of rotted
        photos; Chien-Shiung Wu's deck has two embedded photos."""
        wu = extract_pptx_images(FIXTURES / "Chien-Shiung Wu.pptx")
        assert len(wu) == 2
        for data, ext in wu:
            sniff_image_type(data)  # each is a real image
        noether = extract_pptx_images(FIXTURES / "Emmy Noether.pptx")
        assert len(noether) >= 1
