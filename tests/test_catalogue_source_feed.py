"""Offline tests; no live card websites or user machine paths touched."""
import io
import json
import sqlite3

import pytest
from PIL import Image

from cardscanr_worldwide import source_feed as module


def sample_source():
    return {"id": "tw-official", "region": "TW", "language": "zh-Hant",
            "rights_status": "metadata_only", "archive_policy": "private_research_only",
            "enabled": True, "page_hosts": ["example.org"], "page_paths": ["/tw/card-search/"],
            "image_hosts": ["example.org"], "image_paths": ["/tw/card-img/"],
            "selectors": {"card_links": "a.card", "next_page": "a.next",
                          "card_image": "img.card-art", "fields": {"name": "h1", "collector_number": ".num"}},
            "seeds": [{"kind": "index", "url": "https://example.org/tw/card-search/"}]}


def fake_image():
    output = io.BytesIO()
    Image.new("RGB", (20, 30)).save(output, format="PNG")
    return output.getvalue()


class FakeResponse:
    def __init__(self, data, mime="text/html", status=200):
        self.data = data
        self.status_code = status
        self.headers = {"Content-Type": mime}

    def iter_content(self, chunk_size=65536):
        yield self.data

    def close(self):
        pass


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, **_):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return FakeResponse(b"User-agent: *\nAllow: /\n", "text/plain")
        if url.endswith("/tw/card-search/"):
            return FakeResponse(b'<a class="card" href="/tw/card-search/detail/7/">card</a>')
        if url.endswith("/tw/card-search/detail/7/"):
            return FakeResponse(b'<h1>Bulbasaur</h1><span class="num">007/100</span>'
                                b'<img class="card-art" src="/tw/card-img/7.png">')
        if url.endswith("/tw/card-img/7.png"):
            return FakeResponse(fake_image(), "image/png")
        if url.endswith("/tw/card-img/7.txt"):
            return FakeResponse(b"not an image", "text/html")
        raise AssertionError(f"Unexpected network URL {url}")

    def close(self):
        pass


def test_feed_rejects_untrusted_source_and_urls():
    source = sample_source()
    module.validate_feed({"schema_version": 1, "sources": [source]})
    for url in ["http://example.org/tw/card-search/", "https://127.0.0.1/tw/card-search/",
                "https://evil.org/tw/card-search/", "https://example.org/admin/",
                "https://example.org@evil.org/tw/card-search/", "https://example.org:8443/tw/card-search/"]:
        with pytest.raises(ValueError):
            module._url(url, source["page_hosts"], source["page_paths"])
    with pytest.raises(ValueError):
        module.validate_feed({"schema_version": 1, "sources": [source, source]})
    bad_source = {**source, "archive_policy": "auto_publish"}
    with pytest.raises(ValueError):
        module.validate_feed({"schema_version": 1, "sources": [bad_source]})
    bad_image = {**source, "seeds": [{"kind": "image", "url": "https://example.org/tw/card-img/7.png"}]}
    with pytest.raises(ValueError):
        module.validate_feed({"schema_version": 1, "sources": [bad_image]})


def test_owner_can_feed_exact_sources_without_touching_collector_code(tmp_path):
    path = tmp_path / "feed.json"
    path.write_text(json.dumps({"schema_version": 1, "sources": [sample_source()]}))
    url = "https://example.org/tw/card-search/detail/8/"
    assert module.add_seed(path, "tw-official", "card", url)
    assert not module.add_seed(path, "tw-official", "card", url)
    assert len(module.load_feed(path)[1]["tw-official"]["seeds"]) == 2
    assert module.add_seed(path, "tw-official", "image", "https://example.org/tw/card-img/8.png", "TW:XY:008:regular")
    with pytest.raises(ValueError):
        module.add_seed(path, "tw-official", "image", "https://example.org/tw/card-img/9.png")


def test_one_bounded_batch_captures_html_metadata_and_verified_original_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_public_dns", lambda _: None)
    source = sample_source()
    session = FakeSession()
    with module.FeedRunner(source, tmp_path, session=session) as runner:
        batch = runner.run(2)
        assert batch["attempted"] == 2  # 1 index, 1 detail, with image queued next
        assert batch["queued_remaining"] == 1
        second = runner.run(1)
        assert second["new_images"] == 1
        assert second["queued_remaining"] == 0
    with sqlite3.connect(tmp_path / "source-feed-checkpoint.sqlite") as conn:
        rows = conn.execute("SELECT kind,sha256,relative_path,metadata_json FROM artifacts ORDER BY kind").fetchall()
        assert len(rows) == 3
        art = next(r for r in rows if r[0] == "image")
        raw = (tmp_path / art[2]).read_bytes()
        assert module.digest(raw) == art[1]
        meta = json.loads(art[3])
        assert meta["width"] == 20 and meta["height"] == 30
        assert meta["public_release_approved"] is False
        assert meta["exact_canonical_match"] is False
        assert json.loads(next(r[3] for r in rows if r[0] == "card"))["collector_number"] == "007/100"
    with module.FeedRunner(source, tmp_path, session=FakeSession()) as runner:
        repeat = runner.run(25)
        assert repeat["attempted"] == 0  # resuming never redownloads


def test_disabled_source_cannot_run_or_contact_site(tmp_path):
    source = {**sample_source(), "enabled": False}
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps({"schema_version": 1, "sources": [source]}))
    with pytest.raises(PermissionError):
        module.run_batch(feed, source["id"], tmp_path / "archive")
    assert not (tmp_path / "archive").exists()


def test_fail_closed_disallowed_robots_and_redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_public_dns", lambda _: None)
    class Deny(FakeSession):
        def get(self, url, **kwargs):
            if url.endswith("robots.txt"):
                return FakeResponse(b"User-agent: *\nDisallow: /tw/\n", "text/plain")
            raise AssertionError("Disallowed path should never be fetched")
    with module.FeedRunner(sample_source(), tmp_path, session=Deny()) as runner:
        batch = runner.run(5)
        assert batch["failed"] == 1
        assert batch["succeeded"] == 0
    class Redirect(FakeSession):
        def get(self, url, **kwargs):
            if url.endswith("robots.txt"):
                return FakeResponse(b"User-agent: *\nAllow: /\n", "text/plain")
            return FakeResponse(b"", "text/html", 302)
    with module.FeedRunner(sample_source(), tmp_path / "redirect", session=Redirect()) as runner:
        assert runner.run(5)["failed"] == 1


def test_reject_nonimage_even_when_named_png(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_public_dns", lambda _: None)
    with module.FeedRunner(sample_source(), tmp_path, session=FakeSession()) as runner:
        with pytest.raises(ValueError):
            runner.fetch("image", "https://example.org/tw/card-img/7.txt")


def test_add_source_starts_disabled_with_scope_and_no_network(tmp_path):
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps({"schema_version": 1, "sources": [sample_source()]}))
    module.add_source(feed, source_id="ko-extra", name="Korean secondary", region="KR",
                      language="ko", page_host="cards.example.net", page_path="/tcg/",
                      image_host="img.example.net", image_path="/original/")
    new = module.load_feed(feed)[1]["ko-extra"]
    assert new["enabled"] is False and new["rights_status"] == "permission_pending"
    assert new["seeds"] == []
    assert module.add_seed(feed, "ko-extra", "card", "https://cards.example.net/tcg/7")
    with pytest.raises(ValueError):
        module.add_seed(feed, "ko-extra", "card", "https://cards.example.net/other/7")
    with pytest.raises(ValueError):
        module.add_source(feed, source_id="ko-extra", name="duplicate", region="KR",
                          language="ko", page_host="cards.example.net", page_path="/tcg/",
                          image_host="img.example.net", image_path="/original/")