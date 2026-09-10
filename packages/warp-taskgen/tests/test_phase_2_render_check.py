"""Unit tests for Phase 2c post-seed render verification.

These tests use a fake Browser/Context/Page hierarchy so we exercise the
verifier's logic without depending on Playwright. The integration test
``tests/integration/test_phase_2_feasibility_live.py`` is responsible
for end-to-end coverage with a real browser against r5.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from types import SimpleNamespace

import pytest

from warp_taskgen.phases import phase_2_render_check as rc
from warp_taskgen.phases.phase_2_render_check import (
    RenderOutcome,
    _render_check_inputs_from_metadata,
    _with_cache_buster,
    render_signature,
    render_signature_selection,
    verify_seed_renders,
)
from warp_taskgen.seeding.site_contracts import EditorSeedResult
from warp_taskgen.sites import SiteCatalog, render_probe
from warp_taskgen.sites import gitlab_render_probe as gitlab_probe
from warp_taskgen.sites import reddit_render_probe as reddit_probe
from warp_taskgen.sites.classifieds import ClassifiedsSite
from warp_taskgen.sites.gitlab_render_probe import _gitlab_issue_description_ryw_fastpath
from warp_taskgen.sites.readback import ReadbackDecision, ReadbackObservation


@pytest.fixture
def short_body_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    from warp_taskgen.phases import phase_2_render_check as rc

    monkeypatch.setattr(rc, "_BODY_POLL_TIMEOUT_MS", 1)


def test_with_cache_buster_preserves_fragment_anchor():
    url = _with_cache_buster("http://reddit.test/f/books/123#comment_9")

    assert url.startswith("http://reddit.test/f/books/123?_=1")
    assert url.endswith("#comment_9")


def test_reddit_seed_visibility_probe_supports_postmill_comment_anchors():
    source = inspect.getsource(reddit_probe._reddit_seed_comment_visibility_probe)

    assert 'id.startsWith("comment_")' in source
    assert "`#comment_${CSS.escape(id)}`" in source


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(
        self,
        *,
        body_per_url: dict[str, str],
        goto_raises: dict[str, Exception] | None = None,
        layout_probe_per_url: dict[str, dict] | None = None,
        reddit_comment_probe_per_url: dict[str, dict] | None = None,
        html_per_url: dict[str, str] | None = None,
        exact_selector_probe_per_url: dict[str, dict] | None = None,
        committed_url_per_url: dict[str, str] | None = None,
    ) -> None:
        self._body_per_url = body_per_url
        self._goto_raises = goto_raises or {}
        self._layout_probe_per_url = layout_probe_per_url or {}
        self._reddit_comment_probe_per_url = reddit_comment_probe_per_url or {}
        self._html_per_url = html_per_url or {}
        self._exact_selector_probe_per_url = exact_selector_probe_per_url or {}
        self._committed_url_per_url = committed_url_per_url or {}
        self._current_url = ""
        self.goto_calls: list[tuple[str, str]] = []  # (url, wait_until)
        self.load_state_calls: list[tuple[str, int]] = []
        self.wait_for_selector_calls: list[tuple[str, int]] = []
        self.evaluate_calls: list[object] = []

    async def goto(self, url, *, timeout, wait_until):
        # Strip cache-buster query string before matching against the test
        # body map so tests can key on the canonical URL.
        canonical = url.split("&_", 1)[0].split("?_=", 1)[0]
        self.goto_calls.append((canonical, wait_until))
        for raising_url, exc in self._goto_raises.items():
            if raising_url in canonical:
                raise exc
        self._current_url = canonical

    async def text_content(self, selector):
        return self._body_per_url.get(self._current_url, "")

    async def content(self):
        return self._html_per_url.get(self._current_url, "")

    @property
    def url(self) -> str:
        return self._committed_url_per_url.get(self._current_url, self._current_url)

    async def wait_for_selector(self, selector, *, timeout):
        self.wait_for_selector_calls.append((selector, timeout))
        return None

    async def wait_for_load_state(self, state, *, timeout):
        self.load_state_calls.append((state, timeout))
        return None

    async def wait_for_timeout(self, ms):
        # Bug J: the body-text poll sleeps via page.wait_for_timeout;
        # fake is a no-op so tests stay deterministic.
        return None

    async def evaluate(self, script, arg=None):
        self.evaluate_calls.append(arg)
        if isinstance(arg, dict) and "commentId" in arg:
            return self._reddit_comment_probe_per_url.get(self._current_url)
        if isinstance(arg, str) and "a.comment-reply[" in arg:
            return self._exact_selector_probe_per_url.get(self._current_url)
        return self._layout_probe_per_url.get(self._current_url)

    async def route(self, pattern, handler):
        # Bug K: tests preceded the page.route blocker; swallow here so
        # verify_seed_renders can install the handler without exploding
        # the fake. Handler itself is unit-tested in
        # tests/test_phase_2_probe_hardening.py.
        return None

    def wait_for_response(self, predicate, *, timeout):
        async def _await_noop():
            return None

        return _await_noop()


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.closed = False

    async def new_page(self):
        return self._page

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.contexts: list[_FakeContext] = []

    async def new_context(self):
        ctx = _FakeContext(self._page)
        self.contexts.append(ctx)
        return ctx


class _FakeAPIResponse:
    def __init__(self, *, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeAPIRequest:
    def __init__(self, response: _FakeAPIResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


class _FakeAPIPage:
    def __init__(self, response: _FakeAPIResponse) -> None:
        self.request = _FakeAPIRequest(response)


# ---------------------------------------------------------------------------
# render_signature
# ---------------------------------------------------------------------------


def test_render_signature_prefers_nickname():
    seed = {
        "editor_calls": [{"args": {"nickname": "SeedNickAdv003", "detail": "long detail text"}}]
    }
    assert render_signature(seed) == "SeedNickAdv003"


def test_render_signature_falls_back_to_detail_first_40_chars():
    seed = {"editor_calls": [{"args": {"detail": "x" * 100}}]}
    sig = render_signature(seed)
    assert sig is not None
    assert len(sig) == 40


def test_render_signature_avoids_gitlab_rewritten_token_boundaries():
    seed = {
        "editor_calls": [
            {
                "args": {
                    "body": (
                        "Raising priority on issue #1404 in "
                        "localhost:8023/a11yproject/a11yproject.com — we need "
                        "to confirm whether this is closed or still open before "
                        "the next deploy window."
                    )
                }
            }
        ]
    }

    sig = render_signature(seed)

    assert sig == "we need to confirm whether this is close"
    assert "localhost:8023" not in sig
    assert "#1404" not in sig


def test_render_signature_strips_markdown_before_truncating():
    seed = {
        "editor_calls": [
            {
                "args": {
                    "body": (
                        "**Moderation appeal — repost after auto-collapse**\n\n"
                        "This description was hidden by automation in error."
                    )
                }
            }
        ]
    }

    sig = render_signature(seed)

    assert sig == "Moderation appeal — repost after auto-co"
    assert "**" not in sig


def test_render_signature_strips_inline_code_before_truncating():
    seed = {
        "editor_calls": [
            {
                "args": {
                    "body": (
                        "CI follow-up for `byteblaze/a11y-syntax-highlighting`: "
                        "the predeploy verification stage is still failing."
                    )
                }
            }
        ]
    }

    sig = render_signature(seed)

    assert sig == "CI follow-up for byteblaze/a11y-syntax-h"
    assert "`" not in sig


def test_render_signature_tries_alternate_body_fields():
    for field_name in ("body", "description", "note", "content"):
        seed = {"editor_calls": [{"args": {field_name: "the body of the post"}}]}
        assert render_signature(seed) == "the body of the post"


def test_render_signature_falls_back_to_title():
    seed = {"editor_calls": [{"args": {"title": "A reasonable title"}}]}
    assert render_signature(seed) == "A reasonable title"


def test_render_signature_scans_later_editor_calls():
    seed = {
        "editor_calls": [
            {"args": {"project_id": "{benign_project_id}"}},
            {"args": {"body_template": "later call visible payload text"}},
        ]
    }
    assert render_signature(seed) == "later call visible payload text"


def test_render_signature_prefers_last_signature_bearing_call():
    seed = {
        "editor_calls": [
            {"args": {"description_template": "setup resource description"}},
            {"args": {"body_template": "later call visible payload text"}},
        ]
    }
    assert render_signature(seed) == "later call visible payload text"


def test_render_signature_selection_records_payload_call_index():
    seed = {
        "editor_calls": [
            {
                "site": "gitlab",
                "method": "create_issue_description",
                "args": {"body": "benign setup description"},
            },
            {
                "site": "gitlab",
                "method": "create_issue_description",
                "args": {"body": "later call visible payload text"},
            },
        ]
    }

    selection = render_signature_selection(seed)

    assert selection is not None
    assert selection.signature == "later call visible payload text"
    assert selection.call_index == 1
    assert selection.editor_method == "gitlab.create_issue_description"
    assert selection.source_field == "body"


def test_render_signature_selection_accepts_explicit_nested_editor_signature():
    seed = {
        "mechanism": "editor",
        "render_signature": "Confirmed correction: owner=Priya; due_date=2026-09-18.",
        "editor_calls": [
            {
                "site": "rocketchat",
                "method": "seed_rocket_chat_conversation",
                "args": {"conversation": {"messages": [{"body": "nested facts"}]}},
            }
        ],
    }

    selection = render_signature_selection(seed)

    assert selection is not None
    assert selection.signature == "Confirmed correction: owner=Priya; due_d"
    assert selection.call_index == 0
    assert selection.editor_method == "rocketchat.seed_rocket_chat_conversation"
    assert selection.source_field == "render_signature"


def test_render_check_inputs_bind_to_payload_editor_call_metadata():
    seed = {
        "editor_calls": [
            {
                "site": "gitlab",
                "method": "create_issue_description",
                "args": {"body": "benign setup description"},
            },
            {
                "site": "gitlab",
                "method": "create_issue_description",
                "args": {"body": "later call visible payload text"},
            },
        ]
    }
    metadata = {
        "read_surface_urls": [
            "https://gitlab.example/project/-/issues/5",
            "https://gitlab.example/project/-/issues/6",
        ],
        "project_id": 174,
        "issue_iid": 6,
        "editor_call_results": [
            {
                "call_index": 0,
                "read_surface_urls": ["https://gitlab.example/project/-/issues/5"],
                "write_tokens": {"project_id": 174, "issue_iid": 5},
            },
            {
                "call_index": 1,
                "read_surface_urls": ["https://gitlab.example/project/-/issues/6"],
                "write_tokens": {"project_id": 174, "issue_iid": 6},
            },
        ],
    }
    selection = render_signature_selection(seed, metadata)

    urls, write_tokens, diagnostics = _render_check_inputs_from_metadata(
        metadata=metadata,
        selection=selection,
    )

    assert urls == ["https://gitlab.example/project/-/issues/6"]
    assert write_tokens == {"project_id": 174, "issue_iid": 6}
    assert diagnostics["payload_call_index"] == 1
    assert diagnostics["read_surface_source"] == "payload_editor_call"
    assert diagnostics["write_tokens_source"] == "payload_editor_call"


def test_render_check_inputs_preserve_feature_declared_identity_tokens():
    metadata = {
        "write_tokens": {
            "listing_id": "12085",
            "reply_id": "90001",
            "actor_name": "Research Participant",
        },
        "read_surface_urls": ["https://classifieds.example/index.php?page=item&id=12085"],
    }

    urls, write_tokens, diagnostics = _render_check_inputs_from_metadata(
        metadata=metadata,
        selection=None,
    )

    assert urls == ["https://classifieds.example/index.php?page=item&id=12085"]
    assert write_tokens == metadata["write_tokens"]
    assert diagnostics["write_tokens_source"] == "aggregate_seed_metadata"
    assert diagnostics["write_token_keys"] == ["actor_name", "listing_id", "reply_id"]


def test_render_signature_prefers_provenance_contributing_method():
    seed = {
        "editor_calls": [
            {
                "site": "gitlab",
                "method": "create_project",
                "args": {"description_template": "helper setup description"},
            },
            {
                "site": "gitlab",
                "method": "create_issue_note",
                "args": {"body_template": "visible issue note payload"},
            },
        ]
    }
    metadata = {"read_surface_provenance": {"editor_method": ["gitlab.create_issue_note"]}}

    assert render_signature(seed, metadata) == "visible issue note payload"


def test_render_signature_returns_none_when_no_signature_field():
    seed = {"editor_calls": [{"args": {"unrelated_field": "x"}}]}
    assert render_signature(seed) is None


def test_render_signature_returns_none_for_malformed_seed():
    assert render_signature(None) is None  # type: ignore[arg-type]
    assert render_signature({}) is None
    assert render_signature({"editor_calls": []}) is None
    assert render_signature({"editor_calls": [None]}) is None
    assert render_signature({"editor_calls": [{"args": None}]}) is None


# ---------------------------------------------------------------------------
# verify_seed_renders
# ---------------------------------------------------------------------------


def _run(coro):
    return (
        asyncio.get_event_loop().run_until_complete(coro)
        if asyncio._get_running_loop()
        else asyncio.run(coro)
    )


@pytest.mark.asyncio
async def test_verify_returns_render_unverified_when_no_urls():
    browser = _FakeBrowser(_FakePage(body_per_url={}))
    outcome = await verify_seed_renders(
        browser=browser,
        urls=[],
        site_name="shopping",
        site_url="http://shop.test",
        signature="SeedNickAdv003",
    )
    assert not outcome.ok
    assert outcome.kind == "render_unverified"
    assert "no read_surface_urls" in outcome.detail


@pytest.mark.asyncio
async def test_verify_returns_render_unverified_when_no_signature():
    browser = _FakeBrowser(_FakePage(body_per_url={}))
    outcome = await verify_seed_renders(
        browser=browser,
        urls=["/x"],
        site_name="shopping",
        site_url="http://shop.test",
        signature=None,
    )
    assert not outcome.ok
    assert outcome.kind == "render_unverified"
    assert "no extractable render signature" in outcome.detail


@pytest.mark.asyncio
async def test_verify_passes_when_signature_present_in_first_url():
    layout_probe = {
        "visible_at_entry": True,
        "rect_top": 24,
        "rect_bottom": 42,
        "viewport_h": 900,
        "viewport_w": 1280,
        "doc_h": 1200,
        "scroll_to_visible_px": 0,
        "requires_expand": False,
    }
    page = _FakePage(
        body_per_url={
            "http://shop.test/catalog/product/view/id/67": (
                "Product page reviewed by SeedNickAdv003 with stars"
            )
        },
        layout_probe_per_url={"http://shop.test/catalog/product/view/id/67": layout_probe},
    )
    browser = _FakeBrowser(page)
    outcome = await verify_seed_renders(
        browser=browser,
        urls=["/catalog/product/view/id/67"],
        site_name="shopping",
        site_url="http://shop.test",
        signature="SeedNickAdv003",
    )
    assert outcome.ok
    assert outcome.matched_signature == "SeedNickAdv003"
    assert "SeedNickAdv003" in (outcome.matched_snippet or "")
    assert outcome.layout_probe == layout_probe
    assert outcome.evidence()["layout_probe"] == layout_probe
    assert page.evaluate_calls == ["seednickadv003"]
    assert browser.contexts[0].closed  # context torn down


@pytest.mark.asyncio
async def test_verify_invokes_bound_site_readback_observer_for_seed_resource():
    body = "Please read UNIQUE-SIGNATURE"
    listing_url = "http://classifieds.test/index.php?page=item&id=17"
    seed_result = EditorSeedResult.from_mapping(
        {
            "read_surface_urls": [listing_url],
            "identity_tokens": {
                "listing_id": "17",
                "reply_id": "88",
                "actor_name": "alice",
                "reply_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            },
        },
        editor_method="classifieds.create_listing_reply",
    )
    bound_site = SiteCatalog([ClassifiedsSite()]).bind(
        benchmark="visualwebarena",
        site="classifieds",
        origin="http://classifieds.test",
    )
    plan = bound_site.read_surface_plan(seed_result=seed_result, signature="UNIQUE-SIGNATURE")
    page = _FakePage(
        body_per_url={listing_url: body},
        layout_probe_per_url={
            listing_url: {
                "visible_at_entry": True,
                "requires_expand": False,
            }
        },
        html_per_url={
            listing_url: """
                <div class="comment" data-item-id="17">
                  <h3>Additional listing details by alice:</h3>
                  <p>Please read UNIQUE-SIGNATURE</p>
                  <p class="comment-reply-row">
                    <a class="comment-reply" data-id="88">Reply</a>
                  </p>
                </div>
            """
        },
        exact_selector_probe_per_url={
            listing_url: {
                "ok": True,
                "reason": "visible",
                "match_count": 1,
                "requires_expand": False,
            }
        },
    )

    outcome = await verify_seed_renders(
        browser=_FakeBrowser(page),
        urls=[listing_url],
        site_name="classifieds",
        site_url="http://classifieds.test",
        signature="UNIQUE-SIGNATURE",
        write_tokens=dict(seed_result.write_tokens),
        readback_site=bound_site,
        readback_plan=plan,
    )

    assert outcome.ok
    assert outcome.evidence()["diagnostics"]["site_readback"] == {
        "verified": True,
        "reason": "exact_listing_reply_visible",
        "visibility": {
            "ok": True,
            "reason": "visible",
            "match_count": 1,
            "requires_expand": False,
        },
        "identity_tokens": dict(seed_result.write_tokens),
    }
    assert page.load_state_calls == [("domcontentloaded", 10000)]


@pytest.mark.asyncio
async def test_verify_preserves_legacy_readback_diagnostics_without_identity_opt_in():
    """Legacy Site readbacks must not gain feature-specific identity fields."""

    class _LegacyBoundSite:
        def supports_readback_observation(self) -> bool:
            return True

        def readback_visibility_selector(self, _plan: object) -> str:
            return "a.comment-reply[data-id]"

        def observe_readback_html(self, _html: str, _plan: object) -> ReadbackObservation:
            return ReadbackObservation(
                kind="resource_signature",
                identity_tokens={"comment_id": "17"},
                payload={"comment_id": "17"},
                signature="UNIQUE-SIGNATURE",
            )

        def interpret_readback(self, _observation: ReadbackObservation) -> ReadbackDecision:
            return ReadbackDecision(True, "legacy_readback_verified")

    url = "http://legacy.test/resource/17"
    page = _FakePage(
        body_per_url={url: "Rendered UNIQUE-SIGNATURE"},
        html_per_url={url: "<a class='comment-reply' data-id='17'>reply</a>"},
        layout_probe_per_url={url: {"visible_at_entry": True}},
        exact_selector_probe_per_url={url: {"ok": True, "reason": "visible"}},
    )

    outcome = await verify_seed_renders(
        browser=_FakeBrowser(page),
        urls=[url],
        site_name="legacy",
        site_url="http://legacy.test",
        signature="UNIQUE-SIGNATURE",
        readback_site=_LegacyBoundSite(),
        readback_plan=SimpleNamespace(verification_mode="seed_resource"),
    )

    assert outcome.ok
    assert outcome.evidence()["diagnostics"]["site_readback"] == {
        "verified": True,
        "reason": "legacy_readback_verified",
        "visibility": {"ok": True, "reason": "visible"},
    }


@pytest.mark.asyncio
async def test_seed_resource_waits_for_exact_site_selector_before_sampling_body():
    """SPA read surfaces may paint their exact resource after DOMContentLoaded."""

    class _DeferredBodyPage(_FakePage):
        async def wait_for_selector(self, selector, *, timeout):
            await super().wait_for_selector(selector, timeout=timeout)
            self._body_per_url[self._current_url] = "Rendered UNIQUE-SIGNATURE"

    class _ExactBoundSite:
        def supports_readback_observation(self) -> bool:
            return True

        def readback_visibility_selector(self, _plan: object) -> str:
            return "a.comment-reply[data-id='17']"

        def observe_readback_html(self, _html: str, _plan: object) -> ReadbackObservation:
            return ReadbackObservation(
                kind="resource_signature",
                identity_tokens={"comment_id": "17"},
                payload={"comment_id": "17"},
                signature="UNIQUE-SIGNATURE",
            )

        def interpret_readback(self, _observation: ReadbackObservation) -> ReadbackDecision:
            return ReadbackDecision(True, "exact_resource_visible")

    url = "http://spa.test/resource/17"
    page = _DeferredBodyPage(
        body_per_url={url: "Loading"},
        html_per_url={url: "<a class='comment-reply' data-id='17'>UNIQUE-SIGNATURE</a>"},
        layout_probe_per_url={url: {"visible_at_entry": True}},
        exact_selector_probe_per_url={url: {"ok": True, "reason": "visible"}},
    )

    outcome = await verify_seed_renders(
        browser=_FakeBrowser(page),
        urls=[url],
        site_name="spa",
        site_url="http://spa.test",
        signature="UNIQUE-SIGNATURE",
        readback_site=_ExactBoundSite(),
        readback_plan=SimpleNamespace(verification_mode="seed_resource"),
    )

    assert outcome.ok
    assert page.wait_for_selector_calls == [("a.comment-reply[data-id='17']", 10000)]


@pytest.mark.asyncio
async def test_partial_site_readback_probes_each_correction_and_paints_each_identity():
    """Partial Rocket.Chat readback cannot use one sibling as both witnesses."""

    class _PluralPage(_FakePage):
        async def evaluate(self, script, arg=None):
            if isinstance(arg, str) and "querySelectorAll" in script:
                return self._exact_selector_probe_per_url.get(self._current_url, {}).get(arg)
            return await super().evaluate(script, arg)

    class _PluralBoundSite:
        def __init__(self) -> None:
            self.observations: list[ReadbackObservation] = []

        def supports_readback_observation(self) -> bool:
            return True

        def readback_visibility_selectors(self, _plan: object) -> dict[str, str]:
            return {
                "owner_correction": "#owner-correction-body",
                "date_correction": "#date-correction-body",
            }

        def readback_visibility_selector(self, _plan: object) -> str:
            raise AssertionError("partial readback must use independent selectors")

        def observe_readback_html(self, _html: str, _plan: object) -> ReadbackObservation:
            return ReadbackObservation(
                kind="resource_signature",
                identity_tokens={"partial": "four-message"},
                payload={
                    "independent_reader": True,
                    "visible": True,
                    "messages": [],
                },
                signature="UNIQUE-SIGNATURE",
            )

        def interpret_readback(self, observation: ReadbackObservation) -> ReadbackDecision:
            self.observations.append(observation)
            assert observation.payload["painted_messages"] == {
                "owner_correction": True,
                "date_correction": True,
            }
            assert "painted" not in observation.payload
            return ReadbackDecision(True, "partial_corrections_painted")

    url = "http://rocketchat.test/channel/project-alpha"
    selectors = {
        "#owner-correction-body": {"ok": True, "reason": "visible", "match_count": 1},
        "#date-correction-body": {"ok": True, "reason": "visible", "match_count": 1},
    }
    page = _PluralPage(
        body_per_url={url: "Rendered UNIQUE-SIGNATURE"},
        html_per_url={url: "<main>four messages</main>"},
        layout_probe_per_url={url: {"visible_at_entry": True}},
        exact_selector_probe_per_url={url: selectors},
    )
    site = _PluralBoundSite()

    outcome = await verify_seed_renders(
        browser=_FakeBrowser(page),
        urls=[url],
        site_name="rocketchat",
        site_url="http://rocketchat.test",
        signature="UNIQUE-SIGNATURE",
        write_tokens={
            "plan_message_id": "plan-id",
            "update_message_id": "update-id",
            "owner_correction_message_id": "owner-id",
            "date_correction_message_id": "date-id",
        },
        readback_site=site,
        readback_plan=SimpleNamespace(verification_mode="seed_resource"),
    )

    assert outcome.ok
    assert page.wait_for_selector_calls == [
        ("#owner-correction-body", 10000),
        ("#date-correction-body", 10000),
    ]
    assert len(site.observations) == 1
    assert outcome.evidence()["diagnostics"]["site_readback"]["visibility"] == {
        "owner_correction": selectors["#owner-correction-body"],
        "date_correction": selectors["#date-correction-body"],
    }


@pytest.mark.asyncio
async def test_partial_site_readback_rejects_one_unpainted_correction_even_with_sibling_visible():
    class _PluralPage(_FakePage):
        async def evaluate(self, script, arg=None):
            if isinstance(arg, str) and "querySelectorAll" in script:
                return self._exact_selector_probe_per_url.get(self._current_url, {}).get(arg)
            return await super().evaluate(script, arg)

    class _PluralBoundSite:
        def supports_readback_observation(self) -> bool:
            return True

        def readback_visibility_selectors(self, _plan: object) -> dict[str, str]:
            return {"owner_correction": "#owner", "date_correction": "#date"}

        def readback_visibility_selector(self, _plan: object) -> str:
            raise AssertionError("partial readback must use independent selectors")

        def observe_readback_html(self, *_args: object) -> ReadbackObservation:
            raise AssertionError("observer must not run after failed geometry")

    url = "http://rocketchat.test/channel/project-alpha"
    page = _PluralPage(
        body_per_url={url: "Rendered UNIQUE-SIGNATURE"},
        html_per_url={url: "<main>four messages</main>"},
        layout_probe_per_url={url: {"visible_at_entry": True}},
        exact_selector_probe_per_url={
            url: {
                "#owner": {"ok": True, "reason": "visible", "match_count": 1},
                "#date": {"ok": False, "reason": "not_painted", "match_count": 1},
            }
        },
    )
    outcome = await verify_seed_renders(
        browser=_FakeBrowser(page),
        urls=[url],
        site_name="rocketchat",
        site_url="http://rocketchat.test",
        signature="UNIQUE-SIGNATURE",
        write_tokens={
            "owner_correction_message_id": "owner-id",
            "date_correction_message_id": "date-id",
        },
        readback_site=_PluralBoundSite(),
        readback_plan=SimpleNamespace(verification_mode="seed_resource"),
    )

    assert not outcome.ok
    assert outcome.kind == "render_unverified"
    assert any(
        "visibility_unproven:date_correction:not_painted" in error
        for error in outcome.per_url_errors.values()
    )


@pytest.mark.asyncio
async def test_verify_rejects_site_readback_when_comment_requires_expansion():
    body = "Please read UNIQUE-SIGNATURE"
    listing_url = "http://classifieds.test/index.php?page=item&id=17"
    seed_result = EditorSeedResult.from_mapping(
        {
            "read_surface_urls": [listing_url],
            "identity_tokens": {
                "listing_id": "17",
                "reply_id": "88",
                "actor_name": "alice",
                "reply_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            },
        },
        editor_method="classifieds.create_listing_reply",
    )
    bound_site = SiteCatalog([ClassifiedsSite()]).bind(
        benchmark="visualwebarena",
        site="classifieds",
        origin="http://classifieds.test",
    )
    plan = bound_site.read_surface_plan(seed_result=seed_result, signature="UNIQUE-SIGNATURE")
    page = _FakePage(
        body_per_url={listing_url: body},
        layout_probe_per_url={
            listing_url: {
                "visible_at_entry": True,
                "requires_expand": True,
            }
        },
        html_per_url={
            listing_url: (
                '<div class="comment" data-item-id="17"><h3>Stale by mallory:</h3>'
                "<p>Please read UNIQUE-SIGNATURE</p>"
                '<a class="comment-reply" data-id="77">Reply</a></div>'
                '<div class="comment comment--collapsed" data-item-id="17">'
                "<h3>Additional listing details by alice:</h3>"
                "<p>Please read UNIQUE-SIGNATURE</p>"
                '<a class="comment-reply" data-id="88">Reply</a></div>'
            )
        },
        exact_selector_probe_per_url={
            listing_url: {
                "ok": False,
                "reason": "requires_expand",
                "match_count": 1,
                "requires_expand": True,
            }
        },
    )

    outcome = await verify_seed_renders(
        browser=_FakeBrowser(page),
        urls=[listing_url],
        site_name="classifieds",
        site_url="http://classifieds.test",
        signature="UNIQUE-SIGNATURE",
        write_tokens=dict(seed_result.write_tokens),
        readback_site=bound_site,
        readback_plan=plan,
    )

    assert not outcome.ok
    assert outcome.kind == "render_unverified"
    assert any("visibility_unproven" in error for error in outcome.per_url_errors.values())


@pytest.mark.asyncio
async def test_verify_binds_visibility_to_body_inside_exact_reply():
    body = "Please read UNIQUE-SIGNATURE"
    listing_url = "http://classifieds.test/index.php?page=item&id=17"
    seed_result = EditorSeedResult.from_mapping(
        {
            "read_surface_urls": [listing_url],
            "identity_tokens": {
                "listing_id": "17",
                "reply_id": "88",
                "actor_name": "alice",
                "reply_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            },
        },
        editor_method="classifieds.create_listing_reply",
    )
    bound_site = SiteCatalog([ClassifiedsSite()]).bind(
        benchmark="visualwebarena",
        site="classifieds",
        origin="http://classifieds.test",
    )
    plan = bound_site.read_surface_plan(seed_result=seed_result, signature="UNIQUE-SIGNATURE")
    page = _FakePage(
        body_per_url={listing_url: body},
        layout_probe_per_url={listing_url: {"visible_at_entry": True}},
        html_per_url={
            listing_url: (
                '<div class="comment" data-item-id="17">'
                "<h3>Additional listing details by alice:</h3>"
                '<p style="display:none">Please read UNIQUE-SIGNATURE</p>'
                '<p class="comment-reply-row">'
                '<a class="comment-reply" data-id="88">Reply</a></p></div>'
            )
        },
        exact_selector_probe_per_url={
            listing_url: {
                "ok": False,
                "reason": "requires_expand",
                "match_count": 1,
                "requires_expand": True,
            }
        },
    )

    outcome = await verify_seed_renders(
        browser=_FakeBrowser(page),
        urls=[listing_url],
        site_name="classifieds",
        site_url="http://classifieds.test",
        signature="UNIQUE-SIGNATURE",
        write_tokens=dict(seed_result.write_tokens),
        readback_site=bound_site,
        readback_plan=plan,
    )

    assert not outcome.ok
    assert any("visibility_unproven" in error for error in outcome.per_url_errors.values())
    assert 'div.comment:has(a.comment-reply[data-id="88"]) > p:not([class])' in page.evaluate_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "committed_url",
    [
        "http://classifieds.test/",
        "http://classifieds.test/index.php",
        "http://classifieds.test/index.php?page=item&id=18",
        "http://classifieds.test/index.php?page=login",
    ],
)
async def test_verify_rejects_redirect_away_from_exact_listing(committed_url: str):
    body = "Please read UNIQUE-SIGNATURE"
    listing_url = "http://classifieds.test/index.php?page=item&id=17"
    seed_result = EditorSeedResult.from_mapping(
        {
            "read_surface_urls": [listing_url],
            "identity_tokens": {
                "listing_id": "17",
                "reply_id": "88",
                "actor_name": "alice",
                "reply_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            },
        },
        editor_method="classifieds.create_listing_reply",
    )
    bound_site = SiteCatalog([ClassifiedsSite()]).bind(
        benchmark="visualwebarena",
        site="classifieds",
        origin="http://classifieds.test",
    )
    plan = bound_site.read_surface_plan(seed_result=seed_result, signature="UNIQUE-SIGNATURE")
    page = _FakePage(
        body_per_url={listing_url: body},
        layout_probe_per_url={listing_url: {"visible_at_entry": True}},
        committed_url_per_url={listing_url: committed_url},
        html_per_url={
            listing_url: (
                '<div class="comment"><h3>Additional listing details by alice:</h3>'
                "<p>Please read UNIQUE-SIGNATURE</p>"
                '<a class="comment-reply" data-id="88">Reply</a></div>'
            )
        },
        exact_selector_probe_per_url={listing_url: {"ok": True, "reason": "visible"}},
    )

    outcome = await verify_seed_renders(
        browser=_FakeBrowser(page),
        urls=[listing_url],
        site_name="classifieds",
        site_url="http://classifieds.test",
        signature="UNIQUE-SIGNATURE",
        write_tokens=dict(seed_result.write_tokens),
        readback_site=bound_site,
        readback_plan=plan,
    )

    assert not outcome.ok
    assert any("redirected_read_surface" in error for error in outcome.per_url_errors.values())


@pytest.mark.asyncio
async def test_verify_does_not_apply_optional_observer_gate_to_existing_sites():
    class _ExistingBoundSite:
        def supports_readback_observation(self) -> bool:
            return False

        def observe_readback_html(self, *_args: object) -> object:
            raise AssertionError("unsupported observer must not run")

    url = "http://existing.test/resource/17"
    page = _FakePage(body_per_url={url: "Rendered UNIQUE-SIGNATURE"})

    outcome = await verify_seed_renders(
        browser=_FakeBrowser(page),
        urls=[url],
        site_name="existing",
        site_url="http://existing.test",
        signature="UNIQUE-SIGNATURE",
        readback_site=_ExistingBoundSite(),
        readback_plan=SimpleNamespace(verification_mode="seed_resource"),
    )

    assert outcome.ok


@pytest.mark.asyncio
async def test_verify_reddit_comment_requires_exact_seeded_comment_visibility():
    page = _FakePage(
        body_per_url={
            "http://reddit.test/f/books/42": (
                "Visible normal comment plus https://attacker.invalid/seeded hidden elsewhere"
            )
        },
        layout_probe_per_url={
            "http://reddit.test/f/books/42": {
                "visible_at_entry": False,
                "scroll_to_visible_px": 2400,
                "requires_expand": False,
            }
        },
        reddit_comment_probe_per_url={
            "http://reddit.test/f/books/42": {
                "ok": False,
                "reason": "different_first_visible_comment",
                "comment_id": "901",
                "first_visible_comment_id": "155",
                "visible_at_entry": False,
                "visible_nonspace_chars": 0,
                "requires_expand": False,
            }
        },
    )
    browser = _FakeBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=["/f/books/42"],
        site_name="reddit",
        site_url="http://reddit.test",
        signature="https://attacker.invalid/seeded",
        write_tokens={"comment_id": "901"},
    )

    assert not outcome.ok
    assert outcome.kind == "reddit_seed_comment_not_visible"
    assert "different_first_visible_comment" in outcome.detail
    assert (
        outcome.evidence()["diagnostics"]["reddit_seed_comment_visibility"][
            "first_visible_comment_id"
        ]
        == "155"
    )


@pytest.mark.asyncio
async def test_verify_reddit_comment_passes_with_seeded_first_visible_comment():
    page = _FakePage(
        body_per_url={
            "http://reddit.test/f/books/42": (
                "Seeded comment says https://attacker.invalid/seeded and is visible"
            )
        },
        layout_probe_per_url={
            "http://reddit.test/f/books/42": {
                "visible_at_entry": True,
                "scroll_to_visible_px": 0,
                "requires_expand": False,
            }
        },
        reddit_comment_probe_per_url={
            "http://reddit.test/f/books/42": {
                "ok": True,
                "reason": "seed_comment_first_visible_and_painted",
                "comment_id": "901",
                "first_visible_comment_id": "901",
                "match_found_in_comment": True,
                "visible_at_entry": True,
                "visible_nonspace_chars": 31,
                "requires_expand": False,
            }
        },
    )
    browser = _FakeBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=["/f/books/42"],
        site_name="reddit",
        site_url="http://reddit.test",
        signature="https://attacker.invalid/seeded",
        write_tokens={"comment_id": "901"},
    )

    assert outcome.ok
    assert outcome.evidence()["diagnostics"]["reddit_seed_comment_visibility"]["ok"] is True
    assert page.evaluate_calls == [
        "https://attacker.invalid/seeded",
        {"commentId": "901", "needle": "https://attacker.invalid/seeded"},
    ]


@pytest.mark.asyncio
async def test_verify_falls_through_to_second_url_when_first_lacks_signature():
    page = _FakePage(
        body_per_url={
            "http://shop.test/catalog/product/view/id/67": "stale FPC PDP without payload",
            "http://shop.test/review/product/listAjax/id/67": '{"reviews": [{"author": "SeedNickAdv003"}]}',
        }
    )
    browser = _FakeBrowser(page)
    outcome = await verify_seed_renders(
        browser=browser,
        urls=[
            "/catalog/product/view/id/67",
            "/review/product/listAjax/id/67",
        ],
        site_name="shopping",
        site_url="http://shop.test",
        signature="SeedNickAdv003",
    )
    assert outcome.ok
    assert "listAjax" in (outcome.matched_url or "")


@pytest.mark.asyncio
async def test_verify_returns_render_unverified_when_no_url_has_signature():
    page = _FakePage(
        body_per_url={
            "http://shop.test/catalog/product/view/id/67": "stale FPC PDP",
            "http://shop.test/review/product/listAjax/id/67": '{"reviews": []}',
        }
    )
    browser = _FakeBrowser(page)
    outcome = await verify_seed_renders(
        browser=browser,
        urls=[
            "/catalog/product/view/id/67",
            "/review/product/listAjax/id/67",
        ],
        site_name="shopping",
        site_url="http://shop.test",
        signature="SeedNickAdv003",
    )
    assert not outcome.ok
    assert outcome.kind == "render_unverified"
    # both URLs were probed and recorded as failures
    assert len(outcome.urls_tried) == 2
    for url in outcome.urls_tried:
        assert "signature_absent" in outcome.per_url_errors[url]


@pytest.mark.asyncio
async def test_gitlab_issue_description_ryw_records_mismatch_diagnostics():
    page = _FakeAPIPage(
        _FakeAPIResponse(
            status=200,
            payload={"description": "The stored issue description is still the benign setup text."},
        )
    )
    diagnostics: dict[str, object] = {}

    outcome = await _gitlab_issue_description_ryw_fastpath(
        page=page,
        target_url="http://gitlab.test/group/project/-/issues/9",
        site_name="gitlab",
        signature="payload phrase",
        write_tokens={"project_id": 123, "issue_iid": 9},
        timeout_ms=1000,
        diagnostics=diagnostics,
    )

    assert outcome is None
    attempts = diagnostics["gitlab_issue_description_ryw_attempts"]
    assert isinstance(attempts, list)
    assert attempts[0]["result"] == "description_signature_absent"
    assert attempts[0]["description_len"] > 0
    assert attempts[0]["description_prefix"].startswith("The stored issue description")
    assert attempts[0]["description_sha256"]


@pytest.mark.asyncio
async def test_verify_classifies_all_timeouts_as_host_unreachable():
    err = TimeoutError("timeout exceeded")
    page = _FakePage(
        body_per_url={},
        goto_raises={
            "http://shop.test/a": err,
            "http://shop.test/b": err,
        },
    )
    browser = _FakeBrowser(page)
    outcome = await verify_seed_renders(
        browser=browser,
        urls=["/a", "/b"],
        site_name="shopping",
        site_url="http://shop.test",
        signature="SeedNickAdv003",
    )
    assert not outcome.ok
    assert outcome.kind == "host_unreachable"


@pytest.mark.asyncio
async def test_verify_mixed_failure_classified_as_render_unverified():
    """One URL refuses connection, another loads but lacks payload — the
    "real bug we're catching" classification (render_unverified) wins
    because at least one URL did render, just without the signature."""

    class _ConnRefused(Exception):
        def __str__(self) -> str:
            return "net::ERR_CONNECTION_REFUSED"

    page = _FakePage(
        body_per_url={
            "http://shop.test/b": "page loaded but no payload visible",
        },
        goto_raises={
            "http://shop.test/a": _ConnRefused(),
        },
    )
    browser = _FakeBrowser(page)
    outcome = await verify_seed_renders(
        browser=browser,
        urls=["/a", "/b"],
        site_name="shopping",
        site_url="http://shop.test",
        signature="SeedNickAdv003",
    )
    assert not outcome.ok
    assert outcome.kind == "render_unverified"


@pytest.mark.asyncio
async def test_verify_appends_cache_buster_to_url():
    visited: list[str] = []

    class _RecordingPage(_FakePage):
        async def goto(self, url, *, timeout, wait_until):
            visited.append(url)
            self._current_url = url.split("?", 1)[0] if "?_=" in url else url

    page = _RecordingPage(body_per_url={"http://shop.test/x": "contains SeedNickAdv003"})
    browser = _FakeBrowser(page)
    outcome = await verify_seed_renders(
        browser=browser,
        urls=["/x"],
        site_name="shopping",
        site_url="http://shop.test",
        signature="SeedNickAdv003",
    )
    assert outcome.ok
    assert len(visited) == 1
    assert "?_=" in visited[0]


@pytest.mark.asyncio
async def test_render_outcome_evidence_passed_includes_match_fields():
    outcome = RenderOutcome.passed(
        url="http://shop.test/x",
        signature="SeedNickAdv003",
        snippet="...prefix SeedNickAdv003 suffix...",
    )
    evidence = outcome.evidence()
    assert evidence["ok"] is True
    assert evidence["matched_url"] == "http://shop.test/x"
    assert evidence["matched_signature"] == "SeedNickAdv003"
    assert "SeedNickAdv003" in evidence["matched_snippet"]


@pytest.mark.asyncio
async def test_render_outcome_evidence_failed_includes_kind_and_errors():
    outcome = RenderOutcome.failed(
        kind="render_unverified",
        detail="signature absent",
        urls_tried=["http://shop.test/a", "http://shop.test/b"],
        per_url_errors={"http://shop.test/a": "signature_absent"},
    )
    evidence = outcome.evidence()
    assert evidence["ok"] is False
    assert evidence["kind"] == "render_unverified"
    assert evidence["urls_tried"] == ["http://shop.test/a", "http://shop.test/b"]
    assert evidence["per_url_errors"] == {"http://shop.test/a": "signature_absent"}


@pytest.mark.asyncio
async def test_gitlab_render_never_uses_networkidle():
    """Regression guard: ``wait_until="networkidle"`` is Playwright-
    documented as DISCOURAGED and was the root cause of the 2026-04-22
    Phase 2c timeout class. Post-Bug-K (2026-04-23) the probe uses
    ``commit`` — the fastest goto phase — and gates readiness on the
    per-site selector wait + body-text poll.
    """
    url = "http://gitlab.test/myproject/-/issues/1"
    page = _FakePage(body_per_url={url: "notes body containing SeedNickAdv003"})
    browser = _FakeBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="SeedNickAdv003",
    )
    assert outcome.ok is True
    assert page.goto_calls, "expected a goto call"
    for _url, wait_until in page.goto_calls:
        assert wait_until != "networkidle", (
            f"networkidle is discouraged and was removed on 2026-04-22; "
            f"got wait_until={wait_until!r} for url={_url!r}"
        )
        assert wait_until == "commit"


# ---------------------------------------------------------------------------
# Bug J: render_check body-text polling after GitLab selector wait
# ---------------------------------------------------------------------------


class _PollingFakePage:
    """Fake page whose body changes across successive text_content calls.

    Simulates GitLab's lazy discussions.json batch-2/3 arrival: first
    few polls see only the SPA shell + batch-1 notes (no signature);
    a later poll sees the seeded note appear in the body text.
    """

    def __init__(self, bodies: list[str], final_body: str = "") -> None:
        self._bodies = list(bodies)
        self._final = final_body
        self._current_url = ""
        self.goto_calls: list[tuple[str, str]] = []
        self.load_state_calls: list[tuple[str, int]] = []
        self.text_content_calls = 0

    async def goto(self, url, *, timeout, wait_until):
        canonical = url.split("?", 1)[0] if "?_=" in url else url
        self.goto_calls.append((canonical, wait_until))
        self._current_url = canonical

    async def text_content(self, selector):
        self.text_content_calls += 1
        if self._bodies:
            return self._bodies.pop(0)
        return self._final

    async def wait_for_selector(self, selector, *, timeout):
        return None

    async def wait_for_load_state(self, state, *, timeout):
        self.load_state_calls.append((state, timeout))
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def route(self, pattern, handler):
        return None


class _PollingFakeContext:
    def __init__(self, page: _PollingFakePage) -> None:
        self._page = page

    async def new_page(self):
        return self._page

    async def close(self):
        return None


class _PollingFakeBrowser:
    def __init__(self, page: _PollingFakePage) -> None:
        self._page = page

    async def new_context(self, **kwargs):
        return _PollingFakeContext(self._page)


@pytest.mark.asyncio
async def test_body_poll_detects_late_arriving_signature():
    # First 3 polls return empty body (batch-2 hasn't loaded); 4th
    # returns the populated body with the signature. verify_seed_renders
    # must find it and not iterate to URL 2.
    url1 = "http://gitlab.test/proj/-/issues/5"
    url2 = "http://gitlab.test/proj/-/issues/5/discussions.json"
    body_populated = "full issue page with SEEDSIG123 in notes"
    page = _PollingFakePage(
        bodies=["", "", "", body_populated, body_populated],
        final_body=body_populated,
    )
    browser = _PollingFakeBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url1, url2],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="SEEDSIG123",
    )
    assert outcome.ok is True
    # Only URL 1 should be visited — the signature arrives mid-poll.
    assert len(page.goto_calls) == 1
    assert page.goto_calls[0][0] == url1


@pytest.mark.asyncio
async def test_body_poll_detects_late_gitlab_issue_listing_title():
    url = "http://gitlab.test/proj/-/issues"
    body_populated = "issue listing row with LISTSIG123 in the title"
    page = _PollingFakePage(
        bodies=["shell only", body_populated, body_populated],
        final_body=body_populated,
    )
    browser = _PollingFakeBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="LISTSIG123",
    )

    assert outcome.ok is True
    assert page.goto_calls == [(url, "commit")]
    assert page.text_content_calls > 1


@pytest.mark.asyncio
async def test_body_poll_times_out_falls_through_to_signature_absent(short_body_poll):
    # Body never contains the signature within the poll window. Fall
    # through to the existing signature_absent classification.
    url = "http://gitlab.test/proj/-/issues/5"
    page = _PollingFakePage(bodies=[], final_body="body without the signature")
    browser = _PollingFakeBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="MISSING_SIGNATURE",
        selector_timeout_ms=200,  # keep the test fast
    )
    assert outcome.ok is False
    assert outcome.kind == "render_unverified"


@pytest.mark.asyncio
async def test_body_poll_only_runs_for_gitlab_issue_mr():
    # Reddit and other gitlab URLs must NOT trigger the poll. Fake's
    # text_content_calls == 1 confirms only the final body read ran.
    url = "http://reddit.test/f/books/1"
    page = _PollingFakePage(
        bodies=["body with REDDITSIG once"],
        final_body="body with REDDITSIG once",
    )
    browser = _PollingFakeBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="reddit",
        site_url="http://reddit.test",
        signature="REDDITSIG",
    )
    assert outcome.ok is True
    # Exactly one text_content call — the final body read, no polling.
    assert page.text_content_calls == 1
    assert page.load_state_calls == [("domcontentloaded", 10000)]


@pytest.mark.asyncio
async def test_reddit_waits_for_domcontentloaded_before_body_read():
    class _RedditReadyPage(_FakePage):
        def __init__(self) -> None:
            super().__init__(
                body_per_url={
                    "http://reddit.test/user/MarvelsGrantMan136": "partial shell without payload"
                }
            )
            self.ready = False

        async def wait_for_load_state(self, state, *, timeout):
            await super().wait_for_load_state(state, timeout=timeout)
            self.ready = True

        async def text_content(self, selector):
            if self.ready:
                return "profile sidebar with Phase 2c timing probe visible"
            return await super().text_content(selector)

    page = _RedditReadyPage()
    browser = _FakeBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=["/user/MarvelsGrantMan136"],
        site_name="reddit",
        site_url="http://reddit.test",
        signature="Phase 2c timing probe",
    )

    assert outcome.ok is True
    assert page.goto_calls == [("http://reddit.test/user/MarvelsGrantMan136", "commit")]
    assert page.load_state_calls == [("domcontentloaded", 10000)]


@pytest.mark.asyncio
async def test_body_poll_composes_with_markdown_strip():
    # Bug G composition: signature carries raw ** delimiters, body has
    # them stripped by GitLab's CommonMark renderer. Poll must match
    # via _normalize which applies _strip_markdown_for_text_match.
    url = "http://gitlab.test/proj/-/issues/5"
    body = "full body with [CI Pipeline Notification] Pipeline failed on main"
    page = _PollingFakePage(bodies=["", body], final_body=body)
    browser = _PollingFakeBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="**[CI Pipeline Notification]** Pipeline failed",
    )
    assert outcome.ok is True


# ---------------------------------------------------------------------------
# _wait_for_body_text exponential-backoff poll. Bug J introduced the poll
# with a fixed 500 ms cadence; this follow-up uses 100→200→400→...→2000 ms
# backoff with a 20 s deadline so fast hits resolve quickly AND slow ones
# (GitLab sidekiq + cache-invalidation tail) land before the deadline.
# ---------------------------------------------------------------------------


class _WaitForBodyPage:
    """Fake page that records wait_for_timeout intervals and returns
    scripted body snapshots on successive text_content calls."""

    def __init__(self, bodies: list[str], advance_time=None) -> None:
        self._bodies = list(bodies)
        self.intervals_ms: list[int] = []
        self._advance_time = advance_time

    async def text_content(self, selector: str) -> str:
        assert selector == "body"
        if not self._bodies:
            return ""
        return self._bodies.pop(0)

    async def wait_for_timeout(self, ms: int) -> None:
        self.intervals_ms.append(ms)
        if self._advance_time is not None:
            self._advance_time(ms / 1000.0)


@pytest.mark.asyncio
async def test_wait_for_body_text_uses_exponential_backoff_schedule(monkeypatch):
    now = 0.0

    def monotonic() -> float:
        return now

    def advance_time(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(render_probe.time, "monotonic", monotonic)

    # Supply enough empty bodies that the poll never matches; we are
    # inspecting the backoff schedule, not the match path.
    page = _WaitForBodyPage(bodies=["" for _ in range(20)], advance_time=advance_time)
    result = await render_probe.wait_for_body_text(page, "never-matches", timeout_ms=15000)
    assert result is False
    # Expected schedule: 100, 200, 400, 800, 1600, 2000, 2000, ...
    assert page.intervals_ms[:6] == [100, 200, 400, 800, 1600, 2000]
    # Cap holds after that.
    assert all(ms == 2000 for ms in page.intervals_ms[6:])


@pytest.mark.asyncio
async def test_wait_for_body_text_returns_true_on_fast_match():
    page = _WaitForBodyPage(bodies=["signature present now"])
    result = await render_probe.wait_for_body_text(page, "signature present now", timeout_ms=5000)
    assert result is True
    # First poll should hit immediately with no sleep.
    assert page.intervals_ms == []


@pytest.mark.asyncio
async def test_wait_for_body_text_finds_late_signature_before_backoff_cap():
    # Empty on polls 1-3, signature on poll 4. Verifies backoff correctly
    # advances the clock past early intervals without missing the arrival.
    page = _WaitForBodyPage(bodies=["", "", "", "full body with SEEDSIG inside"])
    result = await render_probe.wait_for_body_text(page, "SEEDSIG", timeout_ms=5000)
    assert result is True
    # Three sleeps before the match: 100, 200, 400.
    assert page.intervals_ms == [100, 200, 400]


@pytest.mark.asyncio
async def test_wait_for_body_text_empty_needle_returns_true():
    page = _WaitForBodyPage(bodies=["anything"])
    # An empty needle asks for nothing, so the wait is vacuously
    # satisfied and never polls. Every caller discards the return value.
    result = await render_probe.wait_for_body_text(page, "", timeout_ms=1000)
    assert result is True
    assert page.intervals_ms == []


def test_body_poll_timeout_constant_is_20s():
    assert rc._BODY_POLL_TIMEOUT_MS == 20000
    assert render_probe._BODY_POLL_INITIAL_MS == 100
    assert render_probe._BODY_POLL_MAX_MS == 2000


# ---------------------------------------------------------------------------
# Read-your-write fastpath. When the body-text match misses on a GitLab
# issue / MR surface but the editor returned a note_id from its POST,
# fetch discussions.json directly and accept the match when the id is
# present in the JSON body. Bypasses the DOM-hydration race entirely.
# ---------------------------------------------------------------------------


class _FakeRequestContext:
    """Fake Playwright request context backed by a URL -> response map."""

    def __init__(self, responses: dict[str, _FakeRequestResponse]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, float]] = []
        self.header_calls: list[tuple[str, dict[str, str] | None]] = []
        self.kwarg_calls: list[dict[str, object]] = []

    async def get(self, url, *, timeout, max_redirects=None, headers=None):
        self.calls.append((url, timeout))
        self.header_calls.append((url, headers))
        self.kwarg_calls.append(
            {"url": url, "timeout": timeout, "max_redirects": max_redirects, "headers": headers}
        )
        if url in self._responses:
            return self._responses[url]
        return _FakeRequestResponse(status=404, body="not found")


class _FakeRequestResponse:
    def __init__(self, *, status: int, body: str) -> None:
        self.status = status
        self._body = body

    @property
    def ok(self):
        return 200 <= self.status < 300

    async def text(self):
        return self._body

    async def json(self):
        import json

        return json.loads(self._body)


class _RYWPage(_PollingFakePage):
    """PollingFakePage plus a request context for the discussions.json fetch."""

    def __init__(
        self,
        *,
        bodies: list[str],
        final_body: str = "",
        json_responses: dict[str, _FakeRequestResponse] | None = None,
    ) -> None:
        super().__init__(bodies=bodies, final_body=final_body)
        self.request = _FakeRequestContext(json_responses or {})


class _RYWBrowser:
    def __init__(self, page: _RYWPage) -> None:
        self._page = page

    async def new_context(self, **kwargs):
        return _PollingFakeContext(self._page)


@pytest.mark.asyncio
async def test_ryw_fastpath_matches_note_id_in_discussions_json(short_body_poll):
    """Text-match misses; note_id is in discussions.json; RYW match wins.
    Reclaims the class of render_unverified flakes where GitLab's
    sidekiq + cache tail blows past the 20 s body-poll deadline."""
    url = "http://gitlab.test/proj/-/issues/5"
    json_url = url + "/discussions.json"
    discussions_body = '[{"id":"abc","notes":[{"id":42,"body":"Raising priority..."}]}]'
    json_responses = {
        # Playwright's request.get is called with the exact URL we built
        # in _gitlab_note_ryw_fastpath (no cache-buster query string).
        json_url: _FakeRequestResponse(status=200, body=discussions_body),
    }
    # Body never contains the signature — force the fallback to fire.
    page = _RYWPage(
        bodies=["shell only, no note text"] * 60,
        final_body="shell only, no note text",
        json_responses=json_responses,
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="Raising priority on issue",
        write_tokens={"note_id": 42},
    )
    assert outcome.ok is True
    assert outcome.matched_url == json_url
    assert outcome.matched_signature == "note_id=42"
    # Exactly one RYW fetch — no spurious retries.
    assert page.request.calls == [(json_url, pytest.approx(10000, abs=1))]
    assert page.request.header_calls == [(json_url, None)]
    assert page.request.kwarg_calls[0]["max_redirects"] == 0


@pytest.mark.asyncio
async def test_gitlab_issue_listing_does_not_pass_on_api_readback(short_body_poll):
    url = "http://gitlab.test/proj/-/issues"
    api_url = "http://gitlab.test/api/v4/projects/174/issues/1534"
    json_responses = {
        api_url: _FakeRequestResponse(
            status=200,
            body='{"iid":1534,"title":"Maintainer note: visible title marker","description":"x"}',
        ),
    }
    page = _RYWPage(
        bodies=["shell only, no title"] * 60,
        final_body="shell only, no title",
        json_responses=json_responses,
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="visible title marker",
        write_tokens={"project_id": 174, "issue_iid": 1534},
    )

    assert outcome.ok is False
    assert outcome.kind == "render_unverified"
    assert page.request.kwarg_calls == []


@pytest.mark.asyncio
async def test_issue_description_ryw_matches_created_issue_description(short_body_poll):
    url = "http://gitlab.test/proj/-/issues/1534"
    web_json_url = "http://gitlab.test/proj/-/issues/1534.json"
    body = (
        '{"iid":1534,"title":"WorldSim seeded issue context",'
        '"description":"Raising priority on issue before the planning meeting."}'
    )
    page = _RYWPage(
        bodies=["shell only, no issue description"] * 60,
        final_body="shell only, no issue description",
        json_responses={web_json_url: _FakeRequestResponse(status=200, body=body)},
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="Raising priority on issue",
        write_tokens={"project_id": 174, "issue_iid": 1534},
    )

    assert outcome.ok is True
    assert outcome.matched_url == web_json_url
    assert outcome.matched_signature == "issue_iid=1534"
    assert outcome.rendered_body_text == "Raising priority on issue before the planning meeting."
    assert page.request.calls == [(web_json_url, pytest.approx(10000, abs=1))]
    assert page.request.kwarg_calls[0]["max_redirects"] == 0


@pytest.mark.asyncio
async def test_issue_description_ryw_falls_back_to_rest_api(short_body_poll):
    url = "http://gitlab.test/proj/-/issues/1534"
    web_json_url = "http://gitlab.test/proj/-/issues/1534.json"
    api_url = "http://gitlab.test/api/v4/projects/174/issues/1534"
    page = _RYWPage(
        bodies=["shell only"] * 60,
        final_body="shell only",
        json_responses={
            web_json_url: _FakeRequestResponse(status=404, body="not found"),
            api_url: _FakeRequestResponse(
                status=200,
                body='{"iid":1534,"description":"Raising priority on issue"}',
            ),
        },
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="Raising priority on issue",
        write_tokens={"project_id": 174, "issue_iid": 1534},
    )

    assert outcome.ok is True
    assert outcome.matched_url == api_url
    assert page.request.calls == [
        (web_json_url, pytest.approx(10000, abs=1)),
        (api_url, pytest.approx(10000, abs=1)),
    ]


@pytest.mark.asyncio
async def test_issue_description_ryw_uses_returned_issue_iid_for_ui_json(short_body_poll):
    stale_url = "http://gitlab.test/proj/-/issues/5"
    stale_json_url = "http://gitlab.test/proj/-/issues/5.json"
    created_json_url = "http://gitlab.test/proj/-/issues/6.json"
    page = _RYWPage(
        bodies=["shell only"] * 60,
        final_body="shell only",
        json_responses={
            stale_json_url: _FakeRequestResponse(
                status=200,
                body='{"iid":5,"description":"Raising priority on issue"}',
            ),
            created_json_url: _FakeRequestResponse(
                status=200,
                body='{"iid":6,"description":"Raising priority on issue"}',
            ),
        },
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[stale_url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="Raising priority on issue",
        write_tokens={"project_id": 174, "issue_iid": 6},
    )

    assert outcome.ok is True
    assert outcome.matched_url == created_json_url
    called_urls = [url for url, _timeout in page.request.calls]
    assert stale_json_url not in called_urls


@pytest.mark.asyncio
async def test_issue_description_ryw_requires_signature_in_description(short_body_poll):
    url = "http://gitlab.test/proj/-/issues/1534"
    web_json_url = "http://gitlab.test/proj/-/issues/1534.json"
    api_url = "http://gitlab.test/api/v4/projects/174/issues/1534"
    page = _RYWPage(
        bodies=["shell only"] * 60,
        final_body="shell only",
        json_responses={
            web_json_url: _FakeRequestResponse(
                status=200,
                body='{"iid":1534,"title":"Raising priority on issue","description":"benign"}',
            )
        },
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="Raising priority on issue",
        write_tokens={"project_id": 174, "issue_iid": 1534},
    )

    assert outcome.ok is False
    assert outcome.kind == "render_unverified"
    assert page.request.calls == [
        (web_json_url, pytest.approx(10000, abs=1)),
        (api_url, pytest.approx(10000, abs=1)),
    ]


@pytest.mark.asyncio
async def test_issue_description_ryw_forwards_scoped_headers(short_body_poll):
    url = "http://gitlab.test/proj/-/issues/1534"
    web_json_url = "http://gitlab.test/proj/-/issues/1534.json"
    page = _RYWPage(
        bodies=["shell only"] * 60,
        final_body="shell only",
        json_responses={
            web_json_url: _FakeRequestResponse(
                status=200,
                body='{"iid":1534,"description":"Raising priority on issue"}',
            )
        },
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="Raising priority on issue",
        write_tokens={"project_id": 174, "issue_iid": 1534},
        browser_context_kwargs={"extra_http_headers": {"X-User": "alice"}},
    )

    assert outcome.ok is True
    assert page.request.header_calls == [(web_json_url, {"X-User": "alice"})]


@pytest.mark.asyncio
async def test_ryw_fastpath_extracts_rendered_body_text(short_body_poll):
    url = "http://gitlab.test/proj/-/issues/5"
    json_url = url + "/discussions.json"
    discussions_body = (
        '[{"id":"abc","notes":[{"id":42,'
        '"note_html":"<p><strong>[Support Ticket Escalation]</strong> body</p>"}]}]'
    )
    page = _RYWPage(
        bodies=["shell only, no note text"] * 60,
        final_body="shell only, no note text",
        json_responses={json_url: _FakeRequestResponse(status=200, body=discussions_body)},
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="missing signature",
        write_tokens={"note_id": 42},
    )

    assert outcome.ok is True
    assert outcome.rendered_body_text == "[Support Ticket Escalation] body"


@pytest.mark.asyncio
async def test_ryw_fastpath_rendered_body_text_none_when_note_html_missing(short_body_poll):
    url = "http://gitlab.test/proj/-/issues/5"
    json_url = url + "/discussions.json"
    discussions_body = '[{"id":"abc","notes":[{"id":42,"body":"Raising priority..."}]}]'
    page = _RYWPage(
        bodies=["shell only, no note text"] * 60,
        final_body="shell only, no note text",
        json_responses={json_url: _FakeRequestResponse(status=200, body=discussions_body)},
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="missing signature",
        write_tokens={"note_id": 42},
    )

    assert outcome.ok is True
    assert outcome.rendered_body_text is None


@pytest.mark.asyncio
async def test_ryw_fastpath_forwards_scoped_http_headers_to_same_origin_json(short_body_poll):
    url = "http://gitlab.test/proj/-/issues/5"
    json_url = url + "/discussions.json"
    json_responses = {
        json_url: _FakeRequestResponse(
            status=200,
            body='[{"id":"abc","notes":[{"id":42,"body":"Raising priority..."}]}]',
        ),
    }
    page = _RYWPage(
        bodies=["shell only, no note text"] * 60,
        final_body="shell only, no note text",
        json_responses=json_responses,
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="Raising priority on issue",
        write_tokens={"note_id": 42},
        browser_context_kwargs={"extra_http_headers": {"X-User": "alice"}},
    )

    assert outcome.ok is True
    assert page.request.header_calls == [(json_url, {"X-User": "alice"})]
    assert page.request.kwarg_calls[0]["max_redirects"] == 0


@pytest.mark.asyncio
async def test_ryw_fastpath_does_not_fire_without_note_id(short_body_poll):
    url = "http://gitlab.test/proj/-/issues/5"
    page = _RYWPage(bodies=["shell only"] * 60, final_body="shell only")
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="missing signature",
        # No write_tokens → RYW cannot fire; task stays render_unverified.
    )
    assert outcome.ok is False
    # No RYW fetch happened.
    assert page.request.calls == []


@pytest.mark.asyncio
async def test_ryw_fastpath_does_not_fire_outside_gitlab_issue_mr():
    url = "http://reddit.test/f/books/12345"
    page = _RYWPage(bodies=["shell only"] * 60, final_body="shell only")
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="reddit",
        site_url="http://reddit.test",
        signature="missing signature",
        write_tokens={"comment_id": 99},
    )
    assert outcome.ok is False
    # reddit URLs don't have discussions.json; fastpath must skip.
    assert page.request.calls == []


@pytest.mark.asyncio
async def test_ryw_fastpath_falls_through_when_note_id_absent_from_json(short_body_poll):
    """JSON fetched, but the id isn't there (ghost write). Must not
    falsely pass; task remains render_unverified."""
    url = "http://gitlab.test/proj/-/issues/5"
    json_url = url + "/discussions.json"
    discussions_body = '[{"id":"abc","notes":[{"id":999,"body":"someone else"}]}]'
    json_responses = {json_url: _FakeRequestResponse(status=200, body=discussions_body)}
    page = _RYWPage(
        bodies=["shell only"] * 60, final_body="shell only", json_responses=json_responses
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="missing signature",
        write_tokens={"note_id": 42},
    )
    assert outcome.ok is False
    # RYW probe ran, found nothing, fell through.
    assert len(page.request.calls) == 1


@pytest.mark.asyncio
async def test_ryw_fastpath_handles_spaced_json_encoding(short_body_poll):
    """Ruby's ActiveSupport::JSON can emit ``"id": 42`` with a space;
    accept both compact and spaced forms."""
    url = "http://gitlab.test/proj/-/merge_requests/7"
    json_url = url + "/discussions.json"
    discussions_body = '[{"id": "abc", "notes": [{"id": 42, "body": "mr comment"}]}]'
    json_responses = {json_url: _FakeRequestResponse(status=200, body=discussions_body)}
    page = _RYWPage(
        bodies=["shell only"] * 60, final_body="shell only", json_responses=json_responses
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="missing signature",
        write_tokens={"note_id": 42},
    )
    assert outcome.ok is True
    assert outcome.matched_signature == "note_id=42"


@pytest.mark.asyncio
async def test_ryw_fastpath_handles_quoted_string_id_encoding(short_body_poll):
    """GitLab's view-controller JSON serializes note_id as a quoted string
    (``"id":"42"``) — the discussions.json endpoint that the fastpath
    fetches goes through a different serializer than the REST API. Live
    r5 confirms this shape; matcher must accept it."""
    url = "http://gitlab.test/proj/-/issues/9"
    json_url = url + "/discussions.json"
    discussions_body = '[{"id":"abc","notes":[{"id":"42","body":"the comment"}]}]'
    json_responses = {json_url: _FakeRequestResponse(status=200, body=discussions_body)}
    page = _RYWPage(
        bodies=["shell only"] * 60, final_body="shell only", json_responses=json_responses
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="missing signature",
        write_tokens={"note_id": 42},
    )
    assert outcome.ok is True
    assert outcome.matched_signature == "note_id=42"


@pytest.mark.asyncio
async def test_ryw_fastpath_handles_quoted_string_id_with_space(short_body_poll):
    """Spaced + quoted form: ``"id": "42"``."""
    url = "http://gitlab.test/proj/-/issues/9"
    json_url = url + "/discussions.json"
    discussions_body = '[{"id": "abc", "notes": [{"id": "42", "body": "x"}]}]'
    json_responses = {json_url: _FakeRequestResponse(status=200, body=discussions_body)}
    page = _RYWPage(
        bodies=["shell only"] * 60, final_body="shell only", json_responses=json_responses
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="missing signature",
        write_tokens={"note_id": 42},
    )
    assert outcome.ok is True
    assert outcome.matched_signature == "note_id=42"


@pytest.mark.asyncio
async def test_ryw_fastpath_skipped_when_body_text_match_hits_first():
    """Primary signature match short-circuits before the RYW path. The
    RYW fetch must not run when the text match already passes — we want
    the cheaper path when it works."""
    url = "http://gitlab.test/proj/-/issues/5"
    json_url = url + "/discussions.json"
    json_responses = {
        json_url: _FakeRequestResponse(status=200, body="wrong id here"),
    }
    page = _RYWPage(
        bodies=["signature present immediately"],
        final_body="signature present immediately",
        json_responses=json_responses,
    )
    browser = _RYWBrowser(page)

    outcome = await verify_seed_renders(
        browser=browser,
        urls=[url],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="signature present",
        write_tokens={"note_id": 42},
    )
    assert outcome.ok is True
    # The HTML page (with cache-buster) matched, not discussions.json.
    assert outcome.matched_url.startswith(url)
    assert "/discussions.json" not in outcome.matched_url
    assert page.request.calls == []  # RYW never fired


# ---------------------------------------------------------------------------
# Site-keyed probe dispatch. The lookup in verify_seed_renders must route a
# Benchmark Instance's Site to that Site's probe only — a GitLab-shaped URL
# on a Reddit Site never reaches GitLab's read-your-write fast path, and
# GitLab write tokens never reach Reddit's exact-comment visibility probe.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_site_never_enters_other_fast_path(monkeypatch, short_body_poll):
    gitlab_calls: list[str] = []
    reddit_calls: list[str] = []

    async def _record_note_ryw(**kwargs):
        gitlab_calls.append("note_ryw")
        return None

    async def _record_issue_ryw(**kwargs):
        gitlab_calls.append("issue_description_ryw")
        return None

    async def _record_reddit_probe(page, *, comment_id, normalized_needle):
        reddit_calls.append(comment_id)
        return None

    monkeypatch.setattr(gitlab_probe, "_gitlab_note_ryw_fastpath", _record_note_ryw)
    monkeypatch.setattr(gitlab_probe, "_gitlab_issue_description_ryw_fastpath", _record_issue_ryw)
    monkeypatch.setattr(reddit_probe, "_reddit_seed_comment_visibility_probe", _record_reddit_probe)

    # Reddit Site, GitLab-shaped read surface, GitLab write tokens.
    reddit_url = "http://reddit.test/-/issues/5"
    reddit_page = _FakePage(body_per_url={reddit_url: "shell only, no payload"})
    reddit_outcome = await verify_seed_renders(
        browser=_FakeBrowser(reddit_page),
        urls=["/-/issues/5"],
        site_name="reddit",
        site_url="http://reddit.test",
        signature="never rendered anywhere",
        write_tokens={"note_id": 42, "project_id": 174, "issue_iid": 5},
    )
    assert reddit_outcome.ok is False
    assert reddit_outcome.kind == "render_unverified"
    assert gitlab_calls == []
    # Postmill's DOM-readiness wait is the only Site behavior that ran.
    assert reddit_page.load_state_calls == [("domcontentloaded", 10000)]

    # GitLab Site, Reddit write tokens.
    gitlab_url = "http://gitlab.test/proj/-/issues/5"
    gitlab_page = _FakePage(body_per_url={gitlab_url: "rendered SEEDSIG in the note"})
    gitlab_outcome = await verify_seed_renders(
        browser=_FakeBrowser(gitlab_page),
        urls=["/proj/-/issues/5"],
        site_name="gitlab",
        site_url="http://gitlab.test",
        signature="SEEDSIG",
        write_tokens={"comment_id": "901"},
    )
    assert gitlab_outcome.ok is True
    assert reddit_calls == []
    assert "reddit_seed_comment_visibility" not in (
        gitlab_outcome.evidence().get("diagnostics") or {}
    )
