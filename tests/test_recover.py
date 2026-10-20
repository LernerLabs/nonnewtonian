"""M7 WP12: optional email recovery of a lost management link.

nonnewtonian.mail.send is monkeypatched to a captured outbox; no real
SMTP server is contacted (that is verified on the server, per the spec)."""

import re

import pytest

from nonnewtonian import db as db_mod
from nonnewtonian.web import create_app, views_recover

NOW = "2026-07-07T00:00:00+00:00"
SMTP = {"SMTP_HOST": "smtp.example.org", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
        "MAIL_FROM": "noreply@example.org"}
TEACHER = "Teacher@Example.edu"


def _make_app(tmp_path, **extra):
    db_path = tmp_path / "r.db"
    conn = db_mod.init_db(db_path, now=NOW)
    conn.execute("INSERT INTO textbooks(slug,title,edition,is_builtin,created_at) "
                 "VALUES('bk','The Book','1st',1,?)", (NOW,))
    conn.commit()
    conn.close()
    cfg = {"DB_PATH": str(db_path), "PHOTO_DIR": str(tmp_path / "photos"),
           "TESTING": True, "WTF_CSRF_ENABLED": False,
           "SITE_URL": "https://nnp.example"}
    cfg.update(extra)
    app = create_app(cfg)
    app._db_path = str(db_path)
    return app


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr("nonnewtonian.mail.send",
                        lambda config, to, subject, body: sent.append((to, subject, body)))
    return sent


@pytest.fixture
def app(tmp_path):
    return _make_app(tmp_path, **SMTP)


@pytest.fixture
def client(app):
    return app.test_client()


def _create_class(client, name="Physics 121", email=TEACHER):
    r = client.post("/new", data={"name": name, "textbook_mode": "builtin",
                                  "textbook_slug": "bk", "teacher_email": email})
    assert r.status_code == 302
    return r.headers["Location"].split("/manage/")[1].split("/")[0]


def _ask(client, email=TEACHER, ip="10.0.0.1", **extra):
    data = {"email": email, "form_ts": "0"}
    data.update(extra)
    return client.post("/recover", data=data, environ_base={"REMOTE_ADDR": ip})


def _confirm_path(body):
    m = re.search(r"https://nnp\.example(/recover/confirm/\S+)", body)
    assert m, body
    return m.group(1)


def test_smtp_off_shows_instructions_and_post_404(tmp_path, outbox):
    client = _make_app(tmp_path).test_client()
    r = client.get("/recover")
    assert r.status_code == 200
    assert b"Email recovery is not turned on" in r.data
    assert _ask(client).status_code == 404
    assert outbox == []


def test_smtp_on_shows_form(client):
    r = client.get("/recover")
    assert r.status_code == 200
    assert b'name="email"' in r.data and b'name="website"' in r.data


def test_known_and_unknown_email_identical(client, outbox):
    _create_class(client)
    known = _ask(client, email="  teacher@example.EDU ", ip="10.0.0.1")
    unknown = _ask(client, email="nobody@example.edu", ip="10.0.0.2")
    assert known.status_code == unknown.status_code == 200
    assert known.data == unknown.data
    assert len(outbox) == 1
    to, subject, body = outbox[0]
    assert to == "teacher@example.EDU"
    assert subject == "Your NonNewtonian Physicists class link"
    assert "https://nnp.example/c/" in body
    assert "/manage/" not in body  # the mail never carries a manage token


def test_confirm_get_does_not_rotate_post_does(client, app, outbox):
    old = _create_class(client)
    _ask(client)
    path = _confirm_path(outbox[0][2])

    r = client.get(path)
    assert r.status_code == 200
    assert b"Your old link will stop working" in r.data
    assert client.get(f"/manage/{old}").status_code == 200  # GET changed nothing

    r = client.post(path)
    assert r.status_code == 302
    new = r.headers["Location"].split("/manage/")[1].split("/")[0]
    assert new != old
    assert client.get(f"/manage/{old}").status_code == 404
    assert client.get(f"/manage/{new}").status_code == 200

    again = client.post(path)
    assert again.status_code == 400
    assert b"already used or has expired" in again.data
    assert client.get(path).status_code == 400
    assert client.get(f"/manage/{new}").status_code == 200


def test_tampered_link_400(client, outbox):
    old = _create_class(client)
    _ask(client)
    path = _confirm_path(outbox[0][2])
    bad = path[:-2] + ("AA" if not path.endswith("AA") else "BB")
    assert client.get(bad).status_code == 400
    assert client.post(bad).status_code == 400
    assert client.get(f"/manage/{old}").status_code == 200


def test_expired_link_400(client, outbox, monkeypatch):
    old = _create_class(client)
    _ask(client)
    path = _confirm_path(outbox[0][2])
    monkeypatch.setattr(views_recover, "CONFIRM_MAX_AGE", -1)
    assert client.get(path).status_code == 400
    assert client.post(path).status_code == 400
    assert client.get(f"/manage/{old}").status_code == 200


def test_honeypot_and_too_fast_400(client, outbox):
    _create_class(client)
    assert _ask(client, website="spam").status_code == 400
    assert client.post("/recover", data={"email": TEACHER, "form_ts": "9999999999"}
                       ).status_code == 400
    assert outbox == []


def test_fourth_request_same_address_429(client, outbox):
    _create_class(client)
    # Different IPs, so the per-IP limit is not what trips.
    for i in range(3):
        assert _ask(client, ip=f"10.0.1.{i}").status_code == 200
    assert _ask(client, email=TEACHER.upper(), ip="10.0.1.9").status_code == 429
    assert len(outbox) == 3


def test_hostile_class_name_single_line_ascii(client, outbox):
    _create_class(client, name="Evil\r\nBcc: x@y.z\n Caf\u00e9 \u2014 " + "A" * 100)
    _ask(client)
    to, subject, body = outbox[0]
    assert subject == "Your NonNewtonian Physicists class link"
    assert body.isascii()
    line = next(ln for ln in body.splitlines() if ln.startswith("Class name"))
    name = line.split(": ", 1)[1]
    assert name.startswith("Evil Bcc: x@y.z Caf")
    assert len(name) == 80
    assert not any(ln.startswith("Bcc") for ln in body.splitlines())


def test_mail_failure_still_identical(client, monkeypatch):
    def boom(*a):
        raise OSError("smtp down")
    monkeypatch.setattr("nonnewtonian.mail.send", boom)
    _create_class(client)
    known = _ask(client, ip="10.0.0.1")
    unknown = _ask(client, email="nobody@example.edu", ip="10.0.0.2")
    assert known.status_code == unknown.status_code == 200
    assert known.data == unknown.data


def test_lost_link_pointers(client):
    token = _create_class(client)
    for path in (f"/manage/{token}/welcome", f"/manage/{token}", "/for-teachers"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert b'href="/recover"' in r.data, path
    assert b"check it carefully" in client.get("/new").data


def test_mail_send_uses_starttls_and_login(monkeypatch):
    # A fake SMTP class: verifies the call sequence only. Real delivery,
    # TLS, and SPF/DKIM are verified on the server (RUNBOOK session 4).
    from nonnewtonian import mail
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg["Subject"], msg.get_content()))

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    cfg = {**SMTP, "SMTP_PORT": 587}
    mail.send(cfg, "t@example.edu", "Subj", "hello\n")
    assert calls == [("connect", "smtp.example.org", 587, 10), ("starttls",),
                     ("login", "u", "p"), ("send", "t@example.edu", "Subj", "hello\n")]
    with pytest.raises(ValueError):  # header injection is refused, not sent
        mail.send(cfg, "t@example.edu", "Subj\r\nBcc: x@y.z", "b")


def test_trap_hits_do_not_spend_the_recipient_budget(client, outbox):
    # M7 review #31: honeypot / too-fast posts used to burn a teacher's 3/day.
    _create_class(client)
    for i in range(4):
        assert _ask(client, website="spam", ip=f"10.0.2.{i}").status_code == 400
    assert _ask(client, ip="10.0.2.9").status_code == 200
    assert len(outbox) == 1


def test_unmatched_addresses_do_not_spend_the_global_budget(client, outbox):
    # Past the 100/day global limit with addresses that match nothing (one
    # IP each, so the per-IP limit never trips); a real teacher still gets mail.
    _create_class(client)
    for i in range(101):
        r = _ask(client, email=f"nobody{i}@example.edu", ip=f"10.1.{i // 250}.{i % 250}")
        assert r.status_code == 200, i
    assert outbox == []
    assert _ask(client, ip="10.2.0.1").status_code == 200
    assert len(outbox) == 1


def test_429_page_shows_the_contact_fallback(client, outbox):
    _create_class(client)
    for i in range(3):
        _ask(client, ip=f"10.0.4.{i}")
    r = _ask(client, ip="10.0.4.9")
    assert r.status_code == 429
    assert b"mailto:mglerner@smith.edu" in r.data and b"Too many requests" in r.data
