"""GET /feedback/mail-account: the link the Jonah app reads for the mail login it sends feedback with."""

from __future__ import annotations

import logging

import pytest

from tests.conftest import make_settings

pytestmark = pytest.mark.anyio

ENV = {"FEEDBACK_SMTP_USER": "sender@gmail.com", "FEEDBACK_SMTP_PASSWORD": "abcd efgh ijkl mnop"}


@pytest.fixture(autouse=True)
def clean_mail_environment(monkeypatch):
    for name in ("FEEDBACK_SMTP_USER", "FEEDBACK_SMTP_PASSWORD", "FEEDBACK_TO_EMAIL"):
        monkeypatch.delenv(name, raising=False)


def configure(monkeypatch, **overrides):
    for name, value in {**ENV, **overrides}.items():
        monkeypatch.setenv(name, value)


async def test_the_login_comes_from_the_environment_with_the_spaces_dropped(make_client, monkeypatch):
    configure(monkeypatch)
    client, _app = await make_client()
    response = await client.get("/feedback/mail-account")
    assert response.status_code == 200
    assert response.json() == {"user": "sender@gmail.com", "password": "abcdefghijklmnop", "to": "cogniaistudios@gmail.com"}
    assert response.headers["cache-control"] == "no-store"


async def test_the_recipient_can_be_changed(make_client, monkeypatch):
    configure(monkeypatch, FEEDBACK_TO_EMAIL="team@example.org")
    client, _app = await make_client()
    assert (await client.get("/feedback/mail-account")).json()["to"] == "team@example.org"


@pytest.mark.parametrize("missing", ["FEEDBACK_SMTP_USER", "FEEDBACK_SMTP_PASSWORD"])
async def test_it_says_so_when_the_mail_login_is_not_set_up(make_client, monkeypatch, missing):
    configure(monkeypatch)
    monkeypatch.delenv(missing)
    client, _app = await make_client()
    response = await client.get("/feedback/mail-account")
    assert response.status_code == 503 and "password" not in response.text.lower().replace("feedback mail", "")
    assert response.headers["cache-control"] == "no-store"


async def test_it_needs_the_apps_key_when_one_is_configured(make_client, monkeypatch):
    configure(monkeypatch)
    client, _app = await make_client(make_settings(app_shared_secret="the-shared-key"))
    assert (await client.get("/feedback/mail-account")).status_code == 401
    assert (await client.get("/feedback/mail-account", headers={"X-Jonah-Key": "wrong"})).status_code == 401
    ok = await client.get("/feedback/mail-account", headers={"X-Jonah-Key": "the-shared-key"})
    assert ok.status_code == 200 and ok.json()["user"] == "sender@gmail.com"


async def test_it_is_rate_limited_like_the_rest_of_the_api(make_client, monkeypatch):
    configure(monkeypatch)
    client, _app = await make_client(make_settings(rate_limit_per_minute=3))
    codes = [(await client.get("/feedback/mail-account")).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


async def test_the_password_is_never_written_to_the_log(make_client, monkeypatch):
    configure(monkeypatch)

    class Collect(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines: list[str] = []

        def emit(self, record):
            self.lines.append(self.format(record) + repr(getattr(record, "fields", "")))

    client, _app = await make_client()
    handler = Collect()
    logging.getLogger().addHandler(handler)  # after create_app(), which replaces the root handlers
    try:
        await client.get("/feedback/mail-account")
    finally:
        logging.getLogger().removeHandler(handler)
    assert handler.lines and not any("abcdefghijklmnop" in line or "abcd efgh" in line for line in handler.lines)


async def test_only_get_is_allowed(make_client, monkeypatch):
    configure(monkeypatch)
    client, _app = await make_client()
    assert (await client.post("/feedback/mail-account")).status_code == 405
