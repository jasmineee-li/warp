"""Phase 2c post-seed render verification.

After ``apply_data_seed_async`` succeeds, fetch the editor's emitted
``read_surface_urls`` in a real browser and confirm a unique signature
of the seeded payload appears in the rendered DOM. Without this check,
Phase 2c's ``feasibility.status="verified"`` only proves the seed write
succeeded — not that the payload renders for a Phase 4 agent. The
2026-04-21 Magento review-pending bug shipped 174 tasks under exactly
that lie; this module is the architectural backstop.

The check is a hard gate by default. To opt out (development without
Playwright installed, or unit tests that mock the seed flow), set
``WORLDSIM_PHASE_2C_SKIP_RENDER_CHECK=1`` — this disables verification
entirely and downgrades the ``verified`` stamp to "API write succeeded
only," which is what the pipeline did before this module existed. Use
it sparingly; production runs should leave it unset.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from warp_taskgen.agent_auth import playwright_storage_state
from warp_taskgen.seeding.site_contracts import normalize_identity_tokens
from warp_taskgen.sites import (
    ReadbackDecision,
    ReadbackFailure,
    ReadbackObservation,
    default_catalog,
)
from warp_taskgen.sites.gitlab_render_probe import GitLabRenderProbe
from warp_taskgen.sites.reddit_render_probe import RedditRenderProbe
from warp_taskgen.sites.render_probe import (
    RenderOutcome,
    normalize_for_text_match,
    strip_markdown_for_text_match,
)

# Phase 2c dispatches Site-specific render behavior through this lookup
# instead of ``site_name == ...`` branches. Sites without an entry keep
# the generic body-text path.
_SITE_RENDER_PROBES: dict[str, Any] = {
    "gitlab": GitLabRenderProbe(),
    "reddit": RedditRenderProbe(),
}

logger = logging.getLogger(__name__)

# Magento PDPs lazy-load the review block via /review/product/listAjax/.
# Arming a wait_for_response on this fragment before page.goto lets us
# block until the AJAX completes; the static PDP HTML alone does not
# contain the review body.
_SHOPPING_LISTAJAX_FRAGMENT = "/review/product/listAjax/"
_SHOPPING_REVIEW_SELECTOR = ".review-items, .no-reviews, #customer-reviews"

# Markers in Playwright exception strings that mean the host is unreachable
# rather than the payload merely missing. These route to host_unreachable
# (loud, explicit infeasibility) instead of render_unverified.
_HOST_UNREACHABLE_MARKERS: tuple[str, ...] = (
    "TimeoutError",
    "ECONNREFUSED",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_CONNECTION_TIMED_OUT",
    "net::ERR_ADDRESS_UNREACHABLE",
)


@dataclass(frozen=True)
class RenderSignatureSelection:
    signature: str
    call_index: int | None = None
    editor_method: str | None = None
    source_field: str | None = None


_GITLAB_REWRITTEN_TEXT_TOKEN_RE = re.compile(
    r"(?ix)"
    # Fully-qualified URLs and bare host/path strings. GitLab may
    # autolink or project-linkify these, changing the visible text.
    r"https?://\S+"
    r"|"
    r"\b(?:localhost|(?:[a-z0-9-]+\.)+[a-z]{2,})(?::\d+)?/[^\s<>\])]+"
    r"|"
    # GitLab references / mentions are also rendered through special
    # reference filters. Keep signatures out of those tokens.
    r"(?<![\w/])(?:\#|!|&)\d+\b"
    r"|"
    r"(?<![\w/])@[a-z0-9_.-]+"
)


def _trim_signature_candidate(text: str) -> str:
    return text.strip(" \t\r\n-\u2013\u2014:;,.()[]{}<>")


def _stable_render_signature_text(text: str, *, limit: int = 40) -> str | None:
    """Pick rendered-stable text without crossing GitLab rewrite tokens."""
    line = strip_markdown_for_text_match(text).split("\n", 1)[0].strip()
    if not line:
        line = text.split("\n", 1)[0].strip()
    if not line:
        return None

    spans: list[str] = []
    cursor = 0
    for match in _GITLAB_REWRITTEN_TEXT_TOKEN_RE.finditer(line):
        spans.append(line[cursor : match.start()])
        cursor = match.end()
    spans.append(line[cursor:])

    candidates = [
        candidate[:limit].rstrip()
        for span in spans
        if len(candidate := _trim_signature_candidate(span)) >= 8
    ]
    if candidates:
        # Prefer the longest stable prose run. This keeps signatures
        # unique when the opening phrase is short or generic and avoids
        # straddling GitLab-autolinked project URLs such as
        # ``localhost:8023/group/project``.
        return max(candidates, key=len)

    # Back-compat fallback for terse payloads where the only available
    # text is itself a URL/reference-like token.
    return line[:limit].rstrip() or text[:limit].rstrip()


def render_signature_selection(
    seed: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> RenderSignatureSelection | None:
    """Select a unique rendered substring and the editor call that owns it.

    Prefers the seed nickname (ASCII, unique by construction in the
    adversarial dataset, and renders in a stable DOM location across
    Magento product reviews / Reddit posts / GitLab notes). Falls back
    in priority order to detail / body / description / note / bio /
    content (a rendered-stable first-line substring capped at 40 chars),
    then title.

    Every base field name is also tried with the ``_template`` and
    ``_text`` suffixes the editor-method binding contract uses for
    free-text template args (e.g. ``description_template``,
    ``bio_text``), so feasibility verification works on any plan that
    targets a method whose body lives on a templated arg.

    A single-call seed whose editor consumes nested structured facts may
    declare ``render_signature`` at the seed boundary. Multi-call seeds must
    continue to derive a call-local signature from arguments so attribution
    cannot become ambiguous. Returns None when no editor call carries a
    signature-bearing field at all — caller treats that as render_unverified
    with a clear "no signature available" message.
    """
    if not isinstance(seed, dict):
        return None
    editor_calls = seed.get("editor_calls")
    if not isinstance(editor_calls, list) or not editor_calls:
        return None
    call_records = [
        (
            index,
            f"{call.get('site')}.{call.get('method')}",
            call.get("args"),
        )
        for index, call in enumerate(editor_calls)
        if isinstance(call, dict) and isinstance(call.get("args"), dict)
    ]
    if not call_records:
        return None
    explicit_signature = seed.get("render_signature")
    if (
        len(call_records) == 1
        and isinstance(explicit_signature, str)
        and explicit_signature.strip()
    ):
        signature = _stable_render_signature_text(explicit_signature.strip())
        if signature:
            index, method, _args = call_records[0]
            return RenderSignatureSelection(
                signature=signature,
                call_index=index,
                editor_method=method,
                source_field="render_signature",
            )
    preferred_methods: set[str] = set()
    provenance = metadata.get("read_surface_provenance") if isinstance(metadata, dict) else None
    if isinstance(provenance, dict):
        methods = provenance.get("editor_method")
        if isinstance(methods, str) and methods.strip():
            preferred_methods.add(methods.strip())
        elif isinstance(methods, list):
            preferred_methods.update(
                str(method).strip()
                for method in methods
                if isinstance(method, str) and method.strip()
            )
    candidate_records = [
        (index, method, args)
        for index, method, args in call_records
        if method in preferred_methods and isinstance(args, dict)
    ]
    if not candidate_records:
        candidate_records = [
            (index, method, args) for index, method, args in call_records if isinstance(args, dict)
        ]
    # In multi-call seeds the last editor call is typically the one that
    # produces the user-visible note/comment while earlier calls create
    # parent resources or helper setup rows. Prefer later calls so a setup
    # title/description cannot overshadow the actually rendered payload.
    candidate_records = list(reversed(candidate_records))

    def _first_nonempty(fields: tuple[str, ...]) -> RenderSignatureSelection | None:
        for index, method, args in candidate_records:
            for base in fields:
                for variant in (base, f"{base}_template", f"{base}_text"):
                    raw = args.get(variant)
                    if isinstance(raw, str) and raw.strip():
                        value = raw.strip()
                        signature = (
                            value if base == "nickname" else _stable_render_signature_text(value)
                        )
                        if signature:
                            return RenderSignatureSelection(
                                signature=signature,
                                call_index=index,
                                editor_method=method,
                                source_field=variant,
                            )
        return None

    nickname = _first_nonempty(("nickname",))
    if nickname is not None:
        return nickname
    body = _first_nonempty(("detail", "body", "description", "note", "bio", "content"))
    if body is not None:
        return body
    title = _first_nonempty(("title", "name"))
    if title is not None:
        return title
    # Fallback: the LLM sometimes invents arg keys (e.g., reddit's
    # ``reply_to_submission_{submission_id}[comment]``) that don't
    # match the binding-spec vocabulary. Pick the longest string
    # value across all args as the signature — by construction this
    # is the rendered body text (tokens like ``{benign_forum_name}``
    # are short, IDs are shorter still). Skip values that look like
    # ``{benign_*}`` tokens entirely so we don't pick up the token
    # reference itself.
    longest: str | None = None
    longest_len = 0
    longest_index: int | None = None
    longest_method: str | None = None
    for index, method, args in candidate_records:
        for value in args.values():
            if not isinstance(value, str):
                continue
            stripped = value.strip()
            if not stripped or stripped.startswith("{benign_"):
                continue
            if len(stripped) > longest_len:
                longest = stripped
                longest_len = len(stripped)
                longest_index = index
                longest_method = method
    if longest is not None and longest_len >= 8:
        signature = _stable_render_signature_text(longest)
        if signature:
            return RenderSignatureSelection(
                signature=signature,
                call_index=longest_index,
                editor_method=longest_method,
                source_field="<longest_string_arg>",
            )
    return None


def render_signature(seed: dict[str, Any], metadata: dict[str, Any] | None = None) -> str | None:
    """Extract a unique substring expected to appear in the rendered DOM."""
    selection = render_signature_selection(seed, metadata)
    return selection.signature if selection is not None else None


async def _layout_probe_for_signature(page: Any, normalized_needle: str) -> dict[str, Any] | None:
    """Return initial-viewport geometry for the best rendered text match."""
    if not normalized_needle:
        return None
    try:
        result = await page.evaluate(
            """
            (needle) => {
              const root = document.body || document.documentElement;
              if (!root) return null;
              const walker = document.createTreeWalker(
                root,
                NodeFilter.SHOW_TEXT,
                {
                  acceptNode(node) {
                    const parent = node.parentElement;
                    if (!parent) return NodeFilter.FILTER_REJECT;
                    const tag = parent.tagName;
                    if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT") {
                      return NodeFilter.FILTER_REJECT;
                    }
                    return NodeFilter.FILTER_ACCEPT;
                  },
                },
              );
              const textNodes = [];
              const charMap = [];
              let corpus = "";
              function appendNormalized(content, nodeIndex) {
                const lower = String(content || "").toLowerCase();
                for (let offset = 0; offset < lower.length; offset += 1) {
                  const ch = lower[offset];
                  if (/\\s/.test(ch)) {
                    if (corpus.length === 0 || corpus[corpus.length - 1] === " ") {
                      continue;
                    }
                    corpus += " ";
                    charMap.push({ nodeIndex, offset });
                    continue;
                  }
                  corpus += ch;
                  charMap.push({ nodeIndex, offset });
                }
              }
              while (walker.nextNode()) {
                const node = walker.currentNode;
                const content = node.textContent || "";
                if (!content) continue;
                const nodeIndex = textNodes.length;
                textNodes.push(node);
                appendNormalized(content, nodeIndex);
              }
              const length = String(needle || "").length;
              const viewportH = window.innerHeight || 0;
              const viewportW = window.innerWidth || 0;
              const scrollY = window.scrollY || 0;
              const doc = document.documentElement || root;
              const docH = Math.max(doc.scrollHeight || 0, root.scrollHeight || 0);

              function probeOccurrence(matchOffset, occurrenceIndex) {
                let startInfo = null;
                let endInfo = null;
                for (let i = 0; i < length; i += 1) {
                  const info = charMap[matchOffset + i];
                  if (!info) continue;
                  if (!startInfo) startInfo = info;
                  endInfo = info;
                }
                if (!startInfo || !endInfo) return null;
                const startNode = textNodes[startInfo.nodeIndex];
                const endNode = textNodes[endInfo.nodeIndex];
                if (!startNode || !endNode) return null;
                const range = document.createRange();
                range.setStart(startNode, startInfo.offset);
                range.setEnd(endNode, Math.min((endInfo.offset || 0) + 1, (endNode.textContent || "").length));
                const rect = range.getBoundingClientRect();
                const visibleAtEntry =
                  rect.width > 0 &&
                  rect.height > 0 &&
                  rect.bottom > 0 &&
                  rect.top < viewportH &&
                  rect.right > 0 &&
                  rect.left < viewportW;
                let requiresExpand = false;
                for (let n = startNode.parentElement; n; n = n.parentElement) {
                  const style = window.getComputedStyle(n);
                  if (style.display === "none" || style.visibility === "hidden") {
                    requiresExpand = true;
                    break;
                  }
                  if (n.tagName === "DETAILS" && !n.open) {
                    requiresExpand = true;
                    break;
                  }
                  if (n.classList && n.classList.contains("comment--collapsed")) {
                    requiresExpand = true;
                    break;
                  }
                }
                return {
                  visible_at_entry: visibleAtEntry,
                  rect_top: rect.top,
                  rect_bottom: rect.bottom,
                  viewport_h: viewportH,
                  viewport_w: viewportW,
                  doc_h: docH,
                  scroll_to_visible_px: visibleAtEntry ? 0 : Math.max(0, rect.top + scrollY - 100),
                  requires_expand: requiresExpand,
                  occurrence_index: occurrenceIndex,
                };
              }

              let matchOffset = corpus.indexOf(String(needle || ""));
              if (matchOffset < 0) return null;
              let fallback = null;
              let occurrenceIndex = 0;
              while (matchOffset >= 0) {
                const candidate = probeOccurrence(matchOffset, occurrenceIndex);
                if (candidate) {
                  if (candidate.visible_at_entry) return candidate;
                  if (!fallback) fallback = candidate;
                  else if (fallback.requires_expand && !candidate.requires_expand) fallback = candidate;
                  else if (
                    fallback.requires_expand === candidate.requires_expand &&
                    candidate.scroll_to_visible_px < fallback.scroll_to_visible_px
                  ) {
                    fallback = candidate;
                  }
                }
                occurrenceIndex += 1;
                matchOffset = corpus.indexOf(
                  String(needle || ""),
                  matchOffset + Math.max(length, 1),
                );
              }
              return fallback;
            }
            """,
            normalized_needle,
        )
    except Exception:
        logger.debug("phase 2c render check: layout probe failed", exc_info=True)
        return None
    return result if isinstance(result, dict) else None


async def _exact_selector_layout_probe(page: Any, selector: str) -> dict[str, Any] | None:
    """Prove one exact Site-owned resource marker is rendered and not hidden."""

    try:
        result = await page.evaluate(
            """
            (selector) => {
              let matches;
              try {
                matches = Array.from(document.querySelectorAll(String(selector || "")));
              } catch (_) {
                return { ok: false, reason: "invalid_selector", match_count: 0 };
              }
              if (matches.length !== 1) {
                return { ok: false, reason: "identity_count", match_count: matches.length };
              }
              const marker = matches[0];
              let requiresExpand = false;
              for (let node = marker; node; node = node.parentElement) {
                const style = window.getComputedStyle(node);
                if (
                  style.display === "none" ||
                  style.visibility === "hidden" ||
                  Number(style.opacity || "1") === 0
                ) {
                  requiresExpand = true;
                  break;
                }
                if (node.tagName === "DETAILS" && !node.open) {
                  requiresExpand = true;
                  break;
                }
                if (node.classList && node.classList.contains("comment--collapsed")) {
                  requiresExpand = true;
                  break;
                }
              }
              const rect = marker.getBoundingClientRect();
              const painted = rect.width > 0 && rect.height > 0;
              return {
                ok: painted && !requiresExpand,
                reason: !painted ? "not_painted" : (requiresExpand ? "requires_expand" : "visible"),
                match_count: 1,
                requires_expand: requiresExpand,
                rect_top: rect.top,
                rect_bottom: rect.bottom,
              };
            }
            """,
            selector,
        )
    except Exception:
        logger.debug("phase 2c exact-resource layout probe failed", exc_info=True)
        return None
    return result if isinstance(result, dict) else None


def _same_committed_render_surface(expected_url: str, committed_url: object) -> bool:
    """Require navigation to remain on the requested route after redirects."""

    if not isinstance(committed_url, str) or not committed_url.strip():
        return False
    try:
        expected = urlsplit(expected_url)
        committed = urlsplit(committed_url)
        expected_query = sorted(
            (key, value)
            for key, value in parse_qsl(expected.query, keep_blank_values=True)
            if key != "_"
        )
        committed_query = sorted(
            (key, value)
            for key, value in parse_qsl(committed.query, keep_blank_values=True)
            if key != "_"
        )
    except (TypeError, ValueError):
        return False
    return (expected.scheme, expected.netloc, expected.path) == (
        committed.scheme,
        committed.netloc,
        committed.path,
    ) and expected_query == committed_query


_WRITE_TOKEN_KEYS: tuple[str, ...] = (
    "note_id",
    "issue_iid",
    "project_id",
    "comment_id",
    "submission_id",
    "review_id",
)


def _write_tokens_from_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    declared = value.get("write_tokens")
    if isinstance(declared, dict):
        try:
            return dict(normalize_identity_tokens(declared))
        except ValueError:
            return {}
    try:
        return dict(normalize_identity_tokens(value))
    except ValueError:
        pass
    write_tokens: dict[str, Any] = {}
    for key in _WRITE_TOKEN_KEYS:
        token_value = value.get(key)
        if token_value not in (None, ""):
            write_tokens[key] = token_value
    return write_tokens


def _editor_call_record_for_selection(
    metadata: dict[str, Any],
    selection: RenderSignatureSelection | None,
) -> dict[str, Any] | None:
    if selection is None or selection.call_index is None:
        return None
    records = metadata.get("editor_call_results")
    if not isinstance(records, list):
        return None
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("call_index") == selection.call_index:
            return record
    return None


def _render_check_inputs_from_metadata(
    *,
    metadata: dict[str, Any],
    selection: RenderSignatureSelection | None,
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """Bind render verification to the payload-bearing editor call.

    Multi-call adversarial seeds are required for Phase 4 because the
    adversarial seed must include benign setup plus the appended attacker
    write. The aggregate metadata remains useful for reporting, but render
    admission should first use the read surfaces and write identifiers from
    the same call that supplied the signature. If per-call metadata is absent
    (older tests/artifacts), fall back to the aggregate contract.
    """
    aggregate_urls = metadata.get("read_surface_urls")
    urls = (
        [url for url in aggregate_urls if isinstance(url, str) and url.strip()]
        if isinstance(aggregate_urls, list)
        else []
    )
    write_tokens = _write_tokens_from_mapping(metadata)
    diagnostics: dict[str, Any] = {}
    if selection is not None:
        diagnostics.update(
            {
                "payload_call_index": selection.call_index,
                "payload_editor_method": selection.editor_method,
                "payload_source_field": selection.source_field,
            }
        )

    call_record = _editor_call_record_for_selection(metadata, selection)
    if call_record is None:
        if write_tokens:
            diagnostics["write_tokens_source"] = "aggregate_seed_metadata"
            diagnostics["write_token_keys"] = sorted(write_tokens)
        return urls, write_tokens, diagnostics

    call_urls = call_record.get("read_surface_urls")
    if isinstance(call_urls, list):
        selected_urls = [url for url in call_urls if isinstance(url, str) and url.strip()]
        if selected_urls:
            urls = selected_urls
            diagnostics["read_surface_source"] = "payload_editor_call"
    call_write_tokens = call_record.get("write_tokens")
    if isinstance(call_write_tokens, dict):
        selected_write_tokens = _write_tokens_from_mapping(call_write_tokens)
        if selected_write_tokens:
            write_tokens = selected_write_tokens
            diagnostics["write_tokens_source"] = "payload_editor_call"
            diagnostics["write_token_keys"] = sorted(selected_write_tokens)
    if "read_surface_source" not in diagnostics:
        diagnostics["read_surface_source"] = "aggregate_seed_metadata"
    if "write_tokens_source" not in diagnostics and write_tokens:
        diagnostics["write_tokens_source"] = "aggregate_seed_metadata"
        diagnostics["write_token_keys"] = sorted(write_tokens)
    return urls, write_tokens, diagnostics


# write-to-visible tail on loaded hosts runs to 5-15 s; the 10 s selector
# timeout catches first-batch note rendering but starves the poll looking
# for a seeded note that arrives in a slow batch 2-3. 20 s gives the
# exponential backoff room to walk its full schedule (100→...→2000 ms)
# before giving up.
_BODY_POLL_TIMEOUT_MS = 20000
# Kept as a compat alias for downstream consumers / tests that referenced
# the old constant. Not used internally after the backoff switch.
_BODY_POLL_INTERVAL_MS = 500


def _resolve_url(url: str, site_url: str) -> str:
    """Coerce any URL to resolve against the instance's site_url.

    - Path-only (``/foo``) → ``{site_url}/foo``.
    - Fully-qualified URL whose host+port matches site_url → unchanged.
    - Fully-qualified URL on a DIFFERENT host+port → rewritten to
      site_url's host+port. This happens when an editor captured the
      platform API's ``web_url`` and the API reflects the internal
      external_url (e.g. ``http://localhost:8023/byteblaze/foo`` on the
      gitlab image). The ProxyingHTTPAdapter's Location-rewrite handles
      this for ``requests`` calls, but Playwright (used here) has no
      such adapter — rewriting in ``_resolve_url`` keeps the render
      check robust across cross-host replay + api-emitted URLs.
    """
    if not url:
        return url
    if url.startswith("/") and site_url:
        return site_url.rstrip("/") + url
    if site_url and (url.startswith("http://") or url.startswith("https://")):
        from urllib.parse import urlsplit, urlunsplit

        src = urlsplit(url)
        dst = urlsplit(site_url)
        if not dst.netloc or src.netloc == dst.netloc:
            return url
        scheme = dst.scheme or src.scheme or "http"
        path = src.path or "/"
        return urlunsplit((scheme, dst.netloc, path, src.query, src.fragment))
    return url


def _with_cache_buster(url: str) -> str:
    """Append a millisecond timestamp to bust stale-response caches.

    Originally motivated by Magento's full-page cache (which serves a
    stale PDP for 60-300s after a status_id flip). GitLab's Rails cache
    and Postmill's CDN-style response headers exhibit the same class of
    staleness when a newly-seeded note / comment is read back through
    the UI before the cache entry expires. Every render-check and
    reachability fetch carries a unique query string; this is cheaper
    than whatever flush-cache endpoint each site exposes and avoids
    perturbing other in-flight tests.

    Applied to UI page navigations only. API endpoints (``/api/v4/...``,
    ``/sv/*.json``) are tolerated by Playwright's GET but should not
    normally be fetched by this module — the render-check and
    reachability probe both navigate HTML pages.
    """
    parts = urlsplit(url)
    query = f"{parts.query}&" if parts.query else ""
    query = f"{query}{urlencode({'_': int(time.time() * 1000)})}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _classify_failure(
    *,
    urls_tried: list[str],
    per_url_errors: dict[str, str],
    signature: str,
    diagnostics: dict[str, Any] | None = None,
) -> RenderOutcome:
    """Decide between host_unreachable and render_unverified.

    Every URL with a network-flavored error → host_unreachable. Any URL
    that loaded successfully but the signature was absent → render_unverified
    (the bug class this module exists to catch).
    """
    if per_url_errors and all(
        any(marker in err for marker in _HOST_UNREACHABLE_MARKERS)
        for err in per_url_errors.values()
    ):
        return RenderOutcome.failed(
            kind="host_unreachable",
            detail=(
                f"all {len(urls_tried)} read_surface_urls timed out or refused "
                "connection during phase 2c render check"
            ),
            urls_tried=urls_tried,
            per_url_errors=per_url_errors,
            diagnostics=diagnostics,
        )
    return RenderOutcome.failed(
        kind="render_unverified",
        detail=(
            f"signature {signature!r} not found in any of {len(urls_tried)} "
            "read_surface_urls — seed wrote to the platform but the payload "
            "is not rendered. Common causes: pending-approval moderation, "
            "draft state, or the editor's read_surface_urls do not point at "
            "the canonical render surface."
        ),
        urls_tried=urls_tried,
        per_url_errors=per_url_errors,
        diagnostics=diagnostics,
    )


async def verify_seed_renders(
    *,
    browser: Any,  # playwright.async_api.Browser
    urls: list[str],
    site_name: str,
    site_url: str,
    signature: str | None,
    nav_timeout_ms: int = 30000,
    selector_timeout_ms: int = 10000,
    storage_state_path: str | None = None,
    browser_context_kwargs: dict[str, Any] | None = None,
    redirect_origin_aliases: tuple[str, ...] = (),
    write_tokens: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    readback_site: Any | None = None,
    readback_plan: Any | None = None,
) -> RenderOutcome:
    """Open a fresh context, try each URL until the signature appears.

    Browser handle is owned by the caller (one per Phase 2c run); this
    function creates and tears down a single context (~200ms each, vs
    1.5-3s for a full launch). Caller is responsible for capping
    concurrent contexts at ~8 to avoid Playwright's eviction edge cases.

    ``browser_context_kwargs`` threads the configured benign user's auth into
    Playwright so private/authed-only pages are checked with the same identity
    Phase 4 uses. ``redirect_origin_aliases`` names exact origins whose
    redirect Locations may be rebound to ``site_url`` before the browser
    follows them. ``storage_state_path`` is retained for older callers.
    """
    if not urls:
        return RenderOutcome.failed(
            kind="render_unverified",
            detail="editor emitted no read_surface_urls — cannot verify rendering",
            urls_tried=[],
            per_url_errors={},
            diagnostics=diagnostics,
        )
    if not signature:
        return RenderOutcome.failed(
            kind="render_unverified",
            detail=(
                "seed has no extractable render signature "
                "(nickname/detail/body/description/note/content/title all absent)"
            ),
            urls_tried=[],
            per_url_errors={},
            diagnostics=diagnostics,
        )

    needle = normalize_for_text_match(signature)
    seen: list[str] = []
    errors: dict[str, str] = {}
    context_kwargs: dict[str, Any] = dict(browser_context_kwargs or {})
    from warp_taskgen.phases.phase_2_reachability import _pop_scoped_extra_http_headers

    scoped_extra_http_headers = _pop_scoped_extra_http_headers(context_kwargs)
    if storage_state_path and "storage_state" not in context_kwargs:
        path_obj = Path(storage_state_path)
        if path_obj.exists():
            storage_state, error = playwright_storage_state(path_obj)
            if error is None:
                context_kwargs["storage_state"] = storage_state
            else:
                return RenderOutcome.failed(
                    kind="auth_unusable",
                    detail=f"storage_state {storage_state_path} is unusable: {error}",
                    urls_tried=[],
                    per_url_errors={},
                    diagnostics=diagnostics,
                )
        else:
            return RenderOutcome.failed(
                kind="auth_missing",
                detail=f"storage_state {storage_state_path} not found",
                urls_tried=[],
                per_url_errors={},
                diagnostics=diagnostics,
            )
    site_probe = _SITE_RENDER_PROBES.get(site_name)
    reddit_comment_id = (
        site_probe.exact_visibility_comment_id(write_tokens) if site_probe is not None else None
    )
    strict_reddit_comment_visibility = reddit_comment_id is not None
    strict_reddit_failure: RenderOutcome | None = None
    context = await browser.new_context(**context_kwargs)
    try:
        page = await context.new_page()
        # Imported lazily to break a potential module-level import cycle
        # with phase_2_reachability (which already imports _with_cache_buster
        # from this module at module load).
        from warp_taskgen.phases.phase_2_reachability import _install_resource_blocker

        await _install_resource_blocker(
            page,
            scoped_extra_http_headers=scoped_extra_http_headers,
            header_scope_url=site_url,
            redirect_origin_aliases=redirect_origin_aliases,
        )
        for raw_url in urls:
            target = _with_cache_buster(_resolve_url(raw_url, site_url))
            seen.append(target)
            try:
                # ``wait_until="commit"`` resolves the moment response
                # headers arrive. Prior ``networkidle`` never settled on
                # SPAs (ActionCable, Gravatar, Sentry); prior
                # ``domcontentloaded`` blocked on deferred JS parse,
                # which under Phase 2c's 64-wide renderer contention
                # tripped the 30 s timeout even when the server returned
                # in <1.4 s. The per-site selector waits below (note list
                # on issue/MR pages, review block on shopping PDPs) are
                # the real readiness signal. If the selector wait fails,
                # the ``text_content`` fallback still runs — the
                # signature check decides.
                await page.goto(target, timeout=nav_timeout_ms, wait_until="commit")
                if site_probe is not None:
                    await site_probe.wait_for_render(
                        page,
                        target_url=target,
                        signature=signature,
                        selector_timeout_ms=selector_timeout_ms,
                        body_poll_timeout_ms=_BODY_POLL_TIMEOUT_MS,
                    )
                if site_name == "shopping" and "/catalog/product/view/" in target:
                    try:
                        await page.wait_for_selector(
                            _SHOPPING_REVIEW_SELECTOR, timeout=selector_timeout_ms
                        )
                    except Exception:
                        pass
                supports_site_observer = getattr(
                    readback_site, "supports_readback_observation", None
                )
                site_readback_observer = (
                    getattr(readback_site, "observe_readback_html", None)
                    if callable(supports_site_observer) and supports_site_observer()
                    else None
                )
                site_seed_resource_readback = (
                    readback_plan is not None
                    and getattr(readback_plan, "verification_mode", None) == "seed_resource"
                    and callable(site_readback_observer)
                )
                visibility_selector: str | None = None
                visibility_selectors: dict[str, str] | None = None
                if site_seed_resource_readback:
                    # Site-owned exact observers supply their own resource
                    # readiness selector.  Waiting for that selector before
                    # sampling body text is necessary for SPA surfaces whose
                    # exact resource paints after DOMContentLoaded; the later
                    # geometry probe remains the independent visibility proof.
                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=selector_timeout_ms
                        )
                    except Exception:
                        pass
                    plural_builder = getattr(readback_site, "readback_visibility_selectors", None)
                    plural = None
                    plural_requested = isinstance(write_tokens, Mapping) and any(
                        key in write_tokens
                        for key in (
                            "owner_correction_message_id",
                            "date_correction_message_id",
                        )
                    )
                    if callable(plural_builder):
                        try:
                            candidate = plural_builder(readback_plan)
                        except Exception as exc:
                            errors[target] = (
                                "site_readback_failed:visibility_selectors_error:"
                                f"{exc.__class__.__name__}"
                            )
                            continue
                        if isinstance(candidate, Mapping) and candidate:
                            normalized = {
                                str(key): value.strip()
                                for key, value in candidate.items()
                                if isinstance(key, str)
                                and key.strip()
                                and isinstance(value, str)
                                and value.strip()
                            }
                            if len(normalized) != len(candidate):
                                errors[target] = "site_readback_failed:invalid_visibility_selectors"
                                continue
                            plural = normalized
                        elif plural_requested:
                            detail = (
                                f"{candidate.reason}:{candidate.detail}"
                                if isinstance(candidate, ReadbackFailure)
                                else "missing plural visibility selectors"
                            )
                            errors[target] = f"site_readback_failed:{detail}"
                            continue
                    elif plural_requested:
                        errors[target] = "site_readback_failed:missing_visibility_selectors"
                        continue
                    if plural:
                        visibility_selectors = plural
                        # Each correction identity has its own selector and
                        # readiness wait.  A single combined selector would
                        # allow one painted row to mask an absent correction.
                        for selector in visibility_selectors.values():
                            try:
                                await page.wait_for_selector(selector, timeout=selector_timeout_ms)
                            except Exception:
                                # The exact geometry probes below own the
                                # fail-closed result; keep body sampling for
                                # useful diagnostics when readiness times out.
                                pass
                    else:
                        selected = readback_site.readback_visibility_selector(readback_plan)
                        if isinstance(selected, ReadbackFailure):
                            errors[target] = (
                                f"site_readback_failed:{selected.reason}:{selected.detail}"
                            )
                            continue
                        visibility_selector = selected
                        try:
                            await page.wait_for_selector(
                                visibility_selector, timeout=selector_timeout_ms
                            )
                        except Exception:
                            # The exact geometry probe below owns the fail-closed
                            # result.  Keep body sampling available for useful
                            # diagnostics when readiness times out.
                            pass
                body_text = await page.text_content("body") or ""
                normalized = normalize_for_text_match(body_text)
                if needle in normalized:
                    pos = normalized.find(needle)
                    raw_pos = 0
                    if pos >= 0 and pos < len(body_text):
                        raw_pos = pos
                    snippet = body_text[max(0, raw_pos - 40) : raw_pos + len(signature) + 40]
                    layout_probe = await _layout_probe_for_signature(page, needle)
                    if site_seed_resource_readback:
                        if not _same_committed_render_surface(target, getattr(page, "url", None)):
                            errors[target] = "site_readback_failed:redirected_read_surface"
                            continue
                        exact_layout_probe: dict[str, Any] | None = None
                        exact_layout_probes: dict[str, dict[str, Any]] = {}
                        if visibility_selectors:
                            for key, selector in visibility_selectors.items():
                                probe = await _exact_selector_layout_probe(page, selector)
                                if not isinstance(probe, dict) or not probe.get("ok"):
                                    reason = (
                                        probe.get("reason", "probe_failed")
                                        if isinstance(probe, dict)
                                        else "probe_failed"
                                    )
                                    errors[target] = (
                                        f"site_readback_failed:visibility_unproven:{key}:{reason}"
                                    )
                                    break
                                exact_layout_probes[key] = probe
                            if len(exact_layout_probes) != len(visibility_selectors):
                                continue
                        else:
                            if not isinstance(visibility_selector, str):
                                errors[target] = "site_readback_failed:missing_visibility_selector"
                                continue
                            exact_layout_probe = await _exact_selector_layout_probe(
                                page, visibility_selector
                            )
                            if not isinstance(
                                exact_layout_probe, dict
                            ) or not exact_layout_probe.get("ok"):
                                reason = (
                                    exact_layout_probe.get("reason", "probe_failed")
                                    if isinstance(exact_layout_probe, dict)
                                    else "probe_failed"
                                )
                                errors[target] = (
                                    f"site_readback_failed:visibility_unproven:{reason}"
                                )
                                continue
                        try:
                            html = await page.content()
                        except Exception as exc:
                            errors[target] = (
                                "site_readback_failed:readback_html_unavailable:"
                                f"{exc.__class__.__name__}"
                            )
                            continue
                        observation = site_readback_observer(html, readback_plan)
                        if isinstance(observation, ReadbackFailure):
                            # Sites without the optional observation capability
                            # retain their established render/readback path.
                            if observation.reason != "unsupported_readback_observation":
                                errors[target] = (
                                    "site_readback_failed:"
                                    f"{observation.reason}:{observation.detail}"
                                )
                                continue
                        elif not isinstance(observation, ReadbackObservation):
                            errors[target] = "site_readback_failed:invalid_readback_observation"
                            continue
                        else:
                            # ``page.content()`` proves exact DOM identity;
                            # it cannot prove paint.  The selector geometry
                            # probe above is the independent witness.  Carry
                            # that witness into the Site-owned observation only
                            # after it succeeds so an HTML-only adapter cannot
                            # manufacture Painted Visibility.
                            if not isinstance(observation.payload, Mapping):
                                errors[target] = "site_readback_failed:malformed_readback_payload"
                                continue
                            observed_payload = dict(observation.payload)
                            if visibility_selectors:
                                # Keep Painted Visibility per exact correction
                                # identity; a global marker would allow an
                                # unrelated painted row to satisfy the gate.
                                observed_payload["painted_messages"] = {
                                    key: True for key in visibility_selectors
                                }
                                observed_payload["visibility_by_message"] = dict(
                                    exact_layout_probes
                                )
                            else:
                                observed_payload.setdefault("painted", True)
                                observed_payload.setdefault("visible", True)
                            observation = ReadbackObservation(
                                kind=observation.kind,
                                identity_tokens=observation.identity_tokens,
                                payload=observed_payload,
                                signature=observation.signature,
                            )
                            decision = readback_site.interpret_readback(observation)
                            if not isinstance(decision, ReadbackDecision) or not decision.verified:
                                reason = (
                                    decision.reason
                                    if isinstance(decision, ReadbackDecision)
                                    else "invalid_readback_decision"
                                )
                                errors[target] = f"site_readback_failed:{reason}"
                                continue
                            readback_diagnostics = dict(diagnostics or {})
                            readback_diagnostics["site_readback"] = {
                                "verified": True,
                                "reason": decision.reason,
                                "visibility": (
                                    dict(exact_layout_probes)
                                    if visibility_selectors
                                    else exact_layout_probe
                                ),
                            }
                            # Keep the historical GitLab/Reddit diagnostics
                            # shape byte-for-byte stable.  Feature-owned plans
                            # may opt in when exact identity is part of their
                            # readback artifact contract explicitly opts in.
                            if getattr(
                                readback_plan,
                                "persist_readback_identity_tokens",
                                False,
                            ):
                                # Identity tokens are a bounded, sanitized
                                # editor contract (IDs, actor label, and body
                                # digest). Persist them beside the painted
                                # witness so the feature artifact proves the
                                # exact resource that was admitted.
                                readback_diagnostics["site_readback"]["identity_tokens"] = dict(
                                    observation.identity_tokens
                                )
                            diagnostics = readback_diagnostics
                    if strict_reddit_comment_visibility:
                        probe = await site_probe.exact_visibility_probe(
                            page,
                            comment_id=reddit_comment_id,
                            normalized_needle=needle,
                        )
                        strict_diagnostics = dict(diagnostics or {})
                        strict_diagnostics["reddit_seed_comment_visibility"] = probe or {
                            "ok": False,
                            "reason": "probe_failed",
                            "comment_id": reddit_comment_id,
                        }
                        bound_readback = readback_site or default_catalog().bind(
                            site=site_probe.site_name,
                            origin=site_url,
                        )
                        decision = bound_readback.interpret_readback(
                            ReadbackObservation(
                                kind="comment_visibility",
                                identity_tokens={"comment_id": reddit_comment_id},
                                payload=probe,
                                signature=signature,
                            )
                        )
                        if not isinstance(decision, ReadbackDecision) or not decision.verified:
                            reason = (
                                decision.reason
                                if isinstance(decision, ReadbackDecision)
                                else "probe_failed"
                            )
                            errors[target] = f"reddit_seed_comment_visibility_failed:{reason}"
                            strict_reddit_failure = RenderOutcome.failed(
                                kind="reddit_seed_comment_not_visible",
                                detail=(
                                    "reddit comment carrier rendered in page text, but the "
                                    f"seeded comment_id {reddit_comment_id!r} was not proven "
                                    f"as the first visible painted comment ({reason})"
                                ),
                                urls_tried=list(seen),
                                per_url_errors=dict(errors),
                                diagnostics=strict_diagnostics,
                            )
                            continue
                        diagnostics = strict_diagnostics
                    return RenderOutcome.passed(
                        url=target,
                        signature=signature,
                        snippet=snippet,
                        rendered_body_text=body_text,
                        layout_probe=layout_probe,
                        diagnostics=diagnostics,
                    )
                # Read-your-write fallback for GitLab note kinds: the text
                # match missed, but the editor returned the authoritative
                # note_id from its POST response. Fetch the discussions.json
                # surface directly via the page's request context (inherits
                # the live session) and look for that exact id in the JSON
                # body. This bypasses every DOM/render-pipeline race at
                # once — sidekiq indexer delay, page-cache invalidation,
                # GFM token rewriting — because we are reading back the
                # same resource we just wrote using the same API that
                # wrote it. On match, report the RYW hit so downstream
                # diagnostics make the source of verification explicit.
                ryw_hit = (
                    await site_probe.read_your_write(
                        page=page,
                        target_url=target,
                        site_name=site_name,
                        signature=signature,
                        write_tokens=write_tokens,
                        timeout_ms=selector_timeout_ms,
                        scoped_extra_http_headers=scoped_extra_http_headers,
                        header_scope_url=site_url,
                        diagnostics=diagnostics,
                        readback_site=readback_site,
                    )
                    if site_probe is not None
                    else None
                )
                if ryw_hit is not None:
                    return ryw_hit
                errors[target] = f"signature_absent (body_len={len(body_text)})"
            except Exception as exc:
                msg = f"{exc.__class__.__name__}: {exc}"
                errors[target] = msg
                logger.debug("phase 2c render check error on %s: %s", target, msg)
        if strict_reddit_failure is not None:
            return strict_reddit_failure
        return _classify_failure(
            urls_tried=seen,
            per_url_errors=errors,
            signature=signature,
            diagnostics=diagnostics,
        )
    finally:
        try:
            await context.close()
        except Exception:
            logger.exception("phase 2c render check failed to close context")
