from __future__ import annotations

import json

import pytest

from tsalert.alerts.discord import DiscordChannel, _fit
from tsalert.alerts.file_sink import FileChannel
from tsalert.sources.base import PermanentSourceError, TransientSourceError


class FakeResponse:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def capturing_transport(response):
    sent = []

    def transport(url, payload):
        sent.append((url, payload))
        if isinstance(response, Exception):
            raise response
        return response

    return transport, sent


# -- discord ------------------------------------------------------------


def test_discord_needs_a_url_to_be_configured():
    assert DiscordChannel("").is_configured() is False
    assert DiscordChannel("https://discord.com/api/webhooks/1/abc").is_configured() is True


@pytest.mark.parametrize("status", [200, 204])
def test_discord_accepts_both_success_codes(status):
    """204 is the documented success, 200 comes back when Discord returns the
    message it created. Treating 200 as a failure would retry a delivery that
    already happened."""
    transport, sent = capturing_transport(FakeResponse(status))
    DiscordChannel("https://hook", transport=transport).send("hello")
    assert len(sent) == 1


def test_discord_suppresses_mentions():
    """Trump's posts contain @ and text that Discord would turn into pings.
    An alert that notifies a whole server every time it fires gets muted."""
    transport, sent = capturing_transport(FakeResponse(204))
    DiscordChannel("https://hook", transport=transport).send("@everyone look")
    assert sent[0][1]["allowed_mentions"] == {"parse": []}


def test_discord_rate_limit_is_transient_and_carries_the_wait():
    transport, _ = capturing_transport(FakeResponse(429, {"retry_after": 2.5}))
    with pytest.raises(TransientSourceError) as exc:
        DiscordChannel("https://hook", transport=transport).send("hi")
    assert exc.value.retry_after == 2.5


def test_discord_server_error_is_transient():
    transport, _ = capturing_transport(FakeResponse(503))
    with pytest.raises(TransientSourceError):
        DiscordChannel("https://hook", transport=transport).send("hi")


@pytest.mark.parametrize("status", [401, 404])
def test_a_deleted_or_wrong_webhook_is_permanent(status):
    """404 is what a deleted webhook returns. Retrying it forever would keep
    a dead channel in the retry queue for the life of the process."""
    transport, _ = capturing_transport(FakeResponse(status))
    with pytest.raises(PermanentSourceError):
        DiscordChannel("https://hook", transport=transport).send("hi")


def test_transport_errors_do_not_leak_the_webhook_url():
    """The URL is the entire credential. HTTP clients routinely put the
    request URL into the exception, and that exception gets logged."""
    secret = "https://discord.com/api/webhooks/123/SUPERSECRETTOKEN"
    transport, _ = capturing_transport(OSError(f"failed connecting to {secret}"))
    with pytest.raises(TransientSourceError) as exc:
        DiscordChannel(secret, transport=transport).send("hi")
    assert "SUPERSECRETTOKEN" not in str(exc.value)


def test_long_alerts_are_trimmed_to_discords_limit():
    """Discord rejects anything over 2000 characters outright, and his posts
    go well past that, so this is normal traffic rather than an edge case."""
    fitted = _fit("x" * 5000)
    assert len(fitted) <= 2000
    assert fitted.endswith("[truncated]")


def test_trimming_keeps_the_head_of_the_alert():
    """Ticker, companies and timestamp are the first lines. Cutting from the
    end costs post text; cutting from the front would cost the answer."""
    alert = "STOCK MENTION: NVDA\ncompanies: Nvidia\n" + ("body " * 1000)
    assert _fit(alert).startswith("STOCK MENTION: NVDA")


# -- file sink ----------------------------------------------------------


def test_file_channel_is_always_configured():
    """The point of this channel: nothing to set up, so nothing to get wrong."""
    assert FileChannel("/tmp/whatever.jsonl").is_configured() is True


def test_file_channel_appends_one_json_line_per_alert(tmp_path):
    path = tmp_path / "alerts.jsonl"
    channel = FileChannel(path)
    channel.send("first")
    channel.send("second")

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["text"] for line in lines] == ["first", "second"]
    assert all(json.loads(line)["written_at"] for line in lines)


def test_file_channel_creates_its_directory(tmp_path):
    """A fresh clone has no data directory until something writes one."""
    channel = FileChannel(tmp_path / "nested" / "deeper" / "alerts.jsonl")
    channel.send("hello")
    assert (tmp_path / "nested" / "deeper" / "alerts.jsonl").exists()


def test_reading_back_gives_newest_first(tmp_path):
    channel = FileChannel(tmp_path / "alerts.jsonl")
    for i in range(5):
        channel.send(f"alert {i}")
    recent = channel.read_recent(limit=3)
    assert [r["text"] for r in recent] == ["alert 4", "alert 3", "alert 2"]


def test_a_truncated_last_line_does_not_lose_the_rest(tmp_path):
    """A crash mid-write leaves a partial final line. Refusing to show any of
    the file because of it would be the wrong trade for an audit trail."""
    path = tmp_path / "alerts.jsonl"
    channel = FileChannel(path)
    channel.send("good one")
    channel.send("good two")
    with path.open("a") as handle:
        handle.write('{"written_at": "2026-08-29T00:00:00+00:00", "te')

    recent = channel.read_recent()
    assert [r["text"] for r in recent] == ["good two", "good one"]


def test_reading_a_file_that_does_not_exist_yet(tmp_path):
    assert FileChannel(tmp_path / "nothing.jsonl").read_recent() == []


def test_newlines_in_a_post_survive_the_round_trip(tmp_path):
    """Alerts are multi line by construction, so the record has to be one
    JSON object per line rather than the raw text."""
    channel = FileChannel(tmp_path / "alerts.jsonl")
    body = "STOCK MENTION: DJT\ncompanies: Trump Media\n\nline one\nline two"
    channel.send(body)
    assert channel.read_recent()[0]["text"] == body
    assert len((tmp_path / "alerts.jsonl").read_text().strip().splitlines()) == 1


# -- pausing a channel --------------------------------------------------


def test_a_paused_channel_is_skipped_and_leaves_no_row(tmp_path):
    """Pausing must not fill the retry queue with alerts nobody asked to send.

    The check runs before claim_alert, so a paused channel leaves no row at
    all. Un-pausing then picks the post up through recover_undelivered, the
    same path a crash before delivery uses.
    """
    from tsalert.alerts.dispatcher import AlertDispatcher
    from tsalert.store import Store
    from test_dispatcher import FakeChannel, make_detection, make_post, no_sleep

    post = make_post()
    live = FakeChannel("console")
    paused = FakeChannel("discord")

    with Store(tmp_path / "paused.db") as store:
        store.upsert_post(post)
        store.save_detection(make_detection(post.id))
        store.set_channel_paused("discord", True)
        dispatcher = AlertDispatcher([live, paused], store, sleep=no_sleep)

        dispatcher.dispatch(post, make_detection(post.id))

        assert len(live.sent) == 1
        assert paused.sent == []
        rows = store._conn.execute("SELECT channel FROM alerts").fetchall()
        assert [r["channel"] for r in rows] == ["console"]


def test_unpausing_delivers_what_was_missed(tmp_path):
    from tsalert.alerts.dispatcher import AlertDispatcher
    from tsalert.store import Store
    from test_dispatcher import FakeChannel, make_detection, make_post, no_sleep

    post = make_post()
    discord = FakeChannel("discord")

    with Store(tmp_path / "unpause.db") as store:
        store.upsert_post(post)
        store.save_detection(make_detection(post.id))
        store.set_channel_paused("discord", True)
        dispatcher = AlertDispatcher([discord], store, sleep=no_sleep)
        dispatcher.dispatch(post, make_detection(post.id))
        assert discord.sent == []

        store.set_channel_paused("discord", False)
        dispatcher.recover_undelivered()
        assert len(discord.sent) == 1


def test_channel_stats_separate_the_failing_channel_from_the_healthy_one(tmp_path):
    """The aggregate hides which channel is broken, which is the one thing
    worth knowing during an outage."""
    from tsalert.alerts.dispatcher import AlertDispatcher
    from tsalert.store import Store
    from test_dispatcher import DownChannel, FakeChannel, make_detection, make_post, no_sleep

    post = make_post()
    with Store(tmp_path / "split.db") as store:
        store.upsert_post(post)
        store.save_detection(make_detection(post.id))
        dispatcher = AlertDispatcher(
            [FakeChannel("console"), DownChannel("discord")], store, sleep=no_sleep
        )
        dispatcher.dispatch(post, make_detection(post.id))

        stats = store.channel_stats()
        assert stats["console"]["delivered"] == 1
        assert stats["discord"]["delivered"] == 0
        assert stats["discord"]["failed"] == 1
        assert "timed out" in (stats["discord"]["last_error"] or "")


# -- the file sink is not infallible, only independent -------------------


def test_a_disk_error_is_transient_not_a_crash(tmp_path):
    """The safety net channel must not be the one that takes the rest down.

    It runs first, and a bare OSError from it escaped the dispatcher, the
    poll loop and run() itself. Worse, the claim row was left pending, so on
    restart retry_failed hit the same channel first and died again: a crash
    loop that never fetched a post. A full disk is transient, like any other
    channel's outage.
    """
    from tsalert.sources.base import TransientSourceError

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(TransientSourceError):
            FileChannel(locked / "alerts.jsonl").send("hello")
    finally:
        locked.chmod(0o700)


def test_a_failing_file_sink_does_not_block_the_other_channels(tmp_path):
    from tsalert.alerts.dispatcher import AlertDispatcher
    from tsalert.store import Store
    from test_dispatcher import FakeChannel, make_detection, make_post, no_sleep

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    post = make_post()
    console = FakeChannel("console")
    try:
        with Store(tmp_path / "blocked.db") as store:
            store.upsert_post(post)
            dispatcher = AlertDispatcher(
                [FileChannel(locked / "a.jsonl"), console], store, sleep=no_sleep
            )
            dispatcher.dispatch(post, make_detection(post.id))

        assert len(console.sent) == 1
    finally:
        locked.chmod(0o700)


def test_a_truncated_multibyte_character_does_not_hide_the_file(tmp_path):
    """A crash mid-write can cut an emoji in half. Strict decoding turned
    that into a UnicodeDecodeError for the whole file, so one bad byte made
    every earlier line unreadable."""
    path = tmp_path / "alerts.jsonl"
    channel = FileChannel(path)
    channel.send("good one")
    with path.open("ab") as handle:
        handle.write(b'{"written_at": "t", "text": "cafe \xf0\x9f')

    assert [r["text"] for r in channel.read_recent()] == ["good one"]


@pytest.mark.parametrize("limit", [0, -1])
def test_a_nonpositive_limit_returns_nothing(tmp_path, limit):
    """records[-0:] is the whole list, which is the opposite of a zero limit."""
    channel = FileChannel(tmp_path / "alerts.jsonl")
    for i in range(3):
        channel.send(f"alert {i}")
    assert channel.read_recent(limit=limit) == []


# -- discord edge cases -------------------------------------------------


def test_a_response_without_a_status_is_transient_not_a_silent_drop():
    """A transport bug is not a verdict from Discord. Treating a missing
    status as permanent discarded the alert forever on a status nobody sent."""
    transport, _ = capturing_transport(object())
    with pytest.raises(TransientSourceError):
        DiscordChannel("https://hook", transport=transport).send("hi")


def test_trimming_counts_the_way_discord_counts():
    """Discord's 2000 limit is on UTF-16 code units, not code points. Emoji
    count double, so trimming to 2000 characters could still produce a 2001
    unit payload, which comes back 400 and is classified permanent: dropped
    for good rather than retried."""
    def utf16_len(text):
        return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)

    for text in ["x" * 5000, "\U0001F600" * 3000, ("\U0001F4C8a" * 2000)]:
        assert utf16_len(_fit(text)) <= 2000


# -- a paused channel must not starve the others ------------------------


def test_a_paused_backlog_does_not_block_another_channels_retries(tmp_path):
    """retryable_alerts has one window shared by every channel, oldest first.
    A paused channel's rows keep their status forever, so a backlog on it sat
    at the head of that window and pushed every other channel out. A pause is
    operator controlled, so unlike a channel that is merely down, this never
    resolved on its own.
    """
    from tsalert.store import Store
    from test_dispatcher import make_detection, make_post

    with Store(tmp_path / "starve.db") as store:
        for i in range(60):
            post = make_post(f"d{i:03d}")
            store.upsert_post(post)
            store.save_detection(make_detection(post.id))
            store.claim_alert(post.id, "discord")
            store.record_alert_result(post.id, "discord", "failed", "down")

        late = make_post("t001")
        store.upsert_post(late)
        store.save_detection(make_detection(late.id))
        store.claim_alert(late.id, "telegram")
        store.record_alert_result(late.id, "telegram", "failed", "down")

        store.set_channel_paused("discord", True)
        retryable = store.retryable_alerts(5)

        assert ("t001", "telegram") in retryable
        assert all(channel != "discord" for _, channel in retryable)
