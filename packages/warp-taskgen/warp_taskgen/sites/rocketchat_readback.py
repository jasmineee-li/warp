"""Feature-local Rocket.Chat rendered-conversation readback.

The named E1 composition targets the pinned TAC Rocket.Chat 5.3 thread-panel
markup.  ``RocketChatThreadPanelReadbackAdapter`` binds that measured markup to
the exact REST message IDs and body hashes emitted by the writer.  The adapter
is still injectable so a deployment with different markup can fail closed
instead of silently falling back to a body-text match.  This module owns the
identity and painted-visibility decision; browser execution owns selector
geometry and the independent reader context.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from warp_taskgen.phase_1.rocket_chat_contracts import (
    ROCKET_CHAT_BENCHMARK,
    ROCKET_CHAT_SITE,
)
from warp_taskgen.sites.readback import (
    ReadbackDecision,
    ReadbackFailure,
    ReadbackObservation,
    identity_token_text,
)
from warp_taskgen.sites.rocketchat_thread_panel import (
    _css_attr_value,
    _expected_message_keys,
    _RocketChatThreadPanelParser,
)


@runtime_checkable
class RocketChatReadbackAdapter(Protocol):
    """Deployment-owned projection of one fresh browser page.

    A live adapter must bind selectors to the exact message identities in the
    read-surface plan.  It may use Playwright, an HTML parser, or another
    browser-side mechanism, but it must return a typed observation rather than
    a boolean/body substring.  The pinned E1 composition supplies the concrete
    ``RocketChatThreadPanelReadbackAdapter`` below; callers may replace it when
    the host's DOM contract changes.
    """

    def readback_visibility_selector(self, plan: Any) -> str | ReadbackFailure: ...

    def readback_visibility_selectors(self, plan: Any) -> Mapping[str, str] | ReadbackFailure: ...

    def observe_readback_html(
        self,
        html: str,
        plan: Any,
    ) -> ReadbackObservation | ReadbackFailure: ...


def _token(identity: Mapping[str, Any], key: str) -> str | None:
    return identity_token_text(identity.get(key))


def _payload_token(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _token(payload, key)
        if value is not None:
            return value
    return None


def _is_true_marker(payload: Mapping[str, Any], *keys: str) -> bool:
    return any(payload.get(key) is True for key in keys)


def _correction_keys(message_keys: tuple[str, ...]) -> tuple[str, ...]:
    """Return correction keys while retaining the legacy single correction."""

    partial = {"owner_correction", "date_correction"}
    if partial.issubset(message_keys):
        return tuple(key for key in message_keys if key in partial)
    if "correction" in message_keys:
        return ("correction",)
    return ()


def _thread_key(identity: Mapping[str, Any], message_keys: tuple[str, ...]) -> str | None:
    """Resolve the logical root key without assuming it is named ``plan``."""

    declared = _token(identity, "thread_key")
    if declared is not None:
        return declared if declared in message_keys else None
    return message_keys[0] if message_keys else None


def _painted_for_key(payload: Mapping[str, Any], key: str) -> bool:
    """Read one independently probed geometry witness from a Site payload."""

    # The render executor supplies ``visibility_by_message`` alongside the
    # convenience booleans.  Prefer the geometry probe and fail if that map
    # exists but omits this key; otherwise a true sibling marker could mask a
    # missing/zero-sized correction.
    geometry_fields = ("visibility_by_message", "message_visibility")
    geometry_seen = False
    for field in geometry_fields:
        values = payload.get(field)
        if not isinstance(values, Mapping):
            continue
        geometry_seen = True
        if key not in values:
            return False
        marker = values[key]
        if marker is True:
            continue
        if not isinstance(marker, Mapping):
            return False
        if marker.get("ok") is not True and marker.get("painted") is not True:
            return False
        if marker.get("requires_expand") is True:
            return False
        reason = marker.get("reason")
        if isinstance(reason, str) and reason.strip().casefold() in {
            "not_painted",
            "requires_expand",
            "hidden",
            "zero_geometry",
        }:
            return False
        for geometry in (
            marker.get("geometry"),
            marker.get("rect"),
            marker,
        ):
            if not isinstance(geometry, Mapping):
                continue
            for width_key in ("width", "rect_width"):
                if width_key in geometry:
                    width = geometry.get(width_key)
                    if isinstance(width, bool) or not isinstance(width, (int, float)) or width <= 0:
                        return False
            for height_key in ("height", "rect_height"):
                if height_key in geometry:
                    height = geometry.get(height_key)
                    if (
                        isinstance(height, bool)
                        or not isinstance(height, (int, float))
                        or height <= 0
                    ):
                        return False
    if geometry_seen:
        return True

    for field in ("painted_by_message", "painted_messages"):
        values = payload.get(field)
        if not isinstance(values, Mapping) or key not in values:
            continue
        marker = values[key]
        if marker is True:
            return True
        if isinstance(marker, Mapping):
            return marker.get("ok") is True or marker.get("painted") is True
        return False
    return False


def _messages(payload: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    raw = payload.get("messages")
    if isinstance(raw, Mapping):
        rows: list[Mapping[str, Any]] = []
        for key, value in raw.items():
            if not isinstance(value, Mapping):
                return None
            # The mapping key is the authoritative logical identity.  A
            # nested ``logical_key`` cannot override it.
            rows.append({**dict(value), "logical_key": key})
        return rows
    if isinstance(raw, (list, tuple)) and all(isinstance(item, Mapping) for item in raw):
        return [item for item in raw if isinstance(item, Mapping)]
    return None


class RocketChatThreadPanelReadbackAdapter:
    """Pinned TAC Rocket.Chat 5.3 exact thread-panel projection.

    The adapter intentionally accepts serialized HTML rather than a browser
    object.  Phase 2c owns browser context/auth and proves the selected body
    has non-zero, visible geometry before accepting the observation.  If the
    panel scope, IDs, author, or body marker drift, parsing returns a typed
    failure and the slice remains inadmissible.
    """

    site = ROCKET_CHAT_SITE
    contract_version = "rocketchat-5.3-thread-panel-v1"
    _panel_selector = (
        ".rcx-thread-view section.contextual-bar__content.flex-tab.threads "
        ".thread-list.js-scroll-thread ul.thread"
    )

    def readback_visibility_selector(self, plan: Any) -> str | ReadbackFailure:
        identity = getattr(plan, "identity_tokens", None)
        correction_id = (
            _css_attr_value(identity.get("correction_message_id"))
            if isinstance(identity, Mapping)
            else None
        )
        if correction_id is None:
            return ReadbackFailure(
                ROCKET_CHAT_SITE,
                "missing_correction_visibility_identity",
                "Rocket.Chat thread readback needs a bounded correction message ID",
            )
        return (
            f"{self._panel_selector} > li[data-qa-id='UserMessage'][data-id='{correction_id}'] "
            "[data-qa-type='message-body']"
        )

    def readback_visibility_selectors(self, plan: Any) -> Mapping[str, str] | ReadbackFailure:
        """Return one exact body selector for each partial correction.

        A mapping keeps the selectors independent all the way to the render
        executor.  Combining them into a CSS union would allow one painted row
        to stand in for the other correction, so that shape is deliberately not
        exposed here.
        """

        identity = getattr(plan, "identity_tokens", None)
        if not isinstance(identity, Mapping):
            return ReadbackFailure(
                self.site,
                "missing_readback_plan",
                "Rocket.Chat plural readback needs identity tokens",
            )
        expected_keys = _expected_message_keys(identity)
        correction_keys = _correction_keys(expected_keys or ())
        if set(correction_keys) != {"owner_correction", "date_correction"}:
            return ReadbackFailure(
                self.site,
                "unsupported_readback_visibility_selectors",
                "Rocket.Chat plural visibility is reserved for the two-correction pilot",
            )
        selectors: dict[str, str] = {}
        for key in correction_keys:
            message_id = _css_attr_value(identity.get(f"{key}_message_id"))
            if message_id is None:
                return ReadbackFailure(
                    self.site,
                    "missing_message_identity",
                    f"Rocket.Chat thread readback needs a bounded {key} message ID",
                )
            selectors[key] = (
                f"{self._panel_selector} > li[data-qa-id='UserMessage'][data-id='{message_id}'] "
                "[data-qa-type='message-body']"
            )
        return selectors

    def observe_readback_html(
        self,
        html: str,
        plan: Any,
    ) -> ReadbackObservation | ReadbackFailure:
        if not isinstance(html, str) or not html.strip():
            return ReadbackFailure(
                ROCKET_CHAT_SITE,
                "malformed_readback_html",
                "ordinary-reader Rocket.Chat HTML is empty",
            )
        identity = getattr(plan, "identity_tokens", None)
        signature = getattr(plan, "signature", None)
        if not isinstance(identity, Mapping) or not isinstance(signature, str) or not signature:
            return ReadbackFailure(
                ROCKET_CHAT_SITE,
                "missing_readback_plan",
                "Rocket.Chat thread readback needs identity tokens and a signature",
            )
        expected_keys = _expected_message_keys(identity)
        if expected_keys is None:
            return ReadbackFailure(
                ROCKET_CHAT_SITE,
                "invalid_message_order",
                "Rocket.Chat thread readback requires a valid logical message order",
            )
        if any(_css_attr_value(identity.get(f"{key}_message_id")) is None for key in expected_keys):
            return ReadbackFailure(
                ROCKET_CHAT_SITE,
                "missing_message_identity",
                "Rocket.Chat thread readback requires every exact message ID",
            )
        expected_by_id = {
            _css_attr_value(identity.get(f"{key}_message_id")): key for key in expected_keys
        }
        if any(key is None for key in expected_by_id):
            return ReadbackFailure(
                ROCKET_CHAT_SITE,
                "invalid_message_identity",
                "Rocket.Chat thread message IDs are not bounded selector values",
            )
        parser = _RocketChatThreadPanelParser()
        try:
            parser.feed(html)
            parser.close()
        except (TypeError, ValueError) as exc:
            return ReadbackFailure(
                ROCKET_CHAT_SITE,
                "malformed_readback_html",
                f"Rocket.Chat thread HTML could not be parsed: {exc}",
            )
        if parser.malformed or parser.frames:
            return ReadbackFailure(
                ROCKET_CHAT_SITE,
                "malformed_thread_panel",
                "Rocket.Chat thread panel had incomplete or invalid identity markup",
            )
        if len(parser.rows) != len(expected_keys):
            return ReadbackFailure(
                ROCKET_CHAT_SITE,
                "message_count_mismatch",
                "Rocket.Chat thread panel did not expose exactly the seeded rows",
            )
        rows: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for parsed in parser.rows:
            key = expected_by_id.get(parsed.message_id)
            if key is None or key in seen_keys:
                return ReadbackFailure(
                    ROCKET_CHAT_SITE,
                    "message_identity_mismatch",
                    "Rocket.Chat thread panel exposed an unknown or duplicate message ID",
                )
            seen_keys.add(key)
            rows.append(
                {
                    "logical_key": key,
                    "message_id": parsed.message_id,
                    "room_id": identity.get("room_id"),
                    "thread_id": parsed.thread_id,
                    "author": parsed.author,
                    "body": parsed.body,
                }
            )
        actual_keys = tuple(row["logical_key"] for row in rows)
        if actual_keys != expected_keys:
            # Without an explicit ``message_order`` token, the host may choose
            # either order for the two independent field corrections.  The
            # rows still carry their own physical IDs and are checked below by
            # the readback interpreter.
            partial_keys = {"owner_correction", "date_correction"}
            if not (
                set(expected_keys) == {"plan", "update", *partial_keys}
                and actual_keys[:2] == ("plan", "update")
                and set(actual_keys[2:]) == partial_keys
            ):
                return ReadbackFailure(
                    ROCKET_CHAT_SITE,
                    "message_order_or_identity_mismatch",
                    "Rocket.Chat thread panel rows were not chronological",
                )
        # The parser deliberately does not manufacture a ``painted`` marker:
        # serialized HTML cannot prove geometry.  The render executor adds that
        # marker only after its exact correction-body layout probe succeeds.
        # The parser itself still fails closed on absent IDs/body markers.
        payload = {
            "room_id": identity.get("room_id"),
            "room_name": identity.get("room_name"),
            "thread_id": identity.get("thread_id"),
            "reader_user_id": identity.get("reader_user_id"),
            "reader_auth_context_id": identity.get("reader_auth_context_id"),
            "independent_reader": True,
            "visible": True,
            "messages": rows,
        }
        return ReadbackObservation(
            kind="resource_signature",
            identity_tokens=identity,
            payload=payload,
            signature=signature,
        )


class RocketChatReadbackCapability:
    """Strict interpretation of an injected Rocket.Chat browser projection."""

    site = ROCKET_CHAT_SITE
    supported_benchmarks = frozenset({ROCKET_CHAT_BENCHMARK})

    def __init__(self, adapter: RocketChatReadbackAdapter | None = None) -> None:
        self._readback_adapter = adapter

    def supports_readback_observation(self) -> bool:
        adapter = self._readback_adapter
        return callable(getattr(adapter, "readback_visibility_selector", None)) and callable(
            getattr(adapter, "observe_readback_html", None)
        )

    def readback_visibility_selector(self, plan: Any) -> str | ReadbackFailure:
        adapter = self._readback_adapter
        selector_builder = getattr(adapter, "readback_visibility_selector", None)
        if not callable(selector_builder):
            return ReadbackFailure(
                self.site,
                "unsupported_readback_visibility",
                "Rocket.Chat has no configured exact painted-message selector",
            )
        try:
            selector = selector_builder(plan)
        except Exception as exc:
            return ReadbackFailure(
                self.site,
                "readback_visibility_error",
                f"{exc.__class__.__name__}: {exc}",
            )
        if isinstance(selector, ReadbackFailure):
            return selector
        if (
            not isinstance(selector, str)
            or not selector.strip()
            or len(selector) > 240
            or "\n" in selector
            or "\r" in selector
        ):
            return ReadbackFailure(
                self.site,
                "invalid_readback_visibility",
                "Rocket.Chat selector must be bounded single-line text",
            )
        return selector.strip()

    def readback_visibility_selectors(self, plan: Any) -> Mapping[str, str] | ReadbackFailure:
        """Forward independent partial-correction selectors from the adapter."""

        adapter = self._readback_adapter
        selector_builder = getattr(adapter, "readback_visibility_selectors", None)
        if not callable(selector_builder):
            return ReadbackFailure(
                self.site,
                "unsupported_readback_visibility_selectors",
                "Rocket.Chat has no configured independent painted-message selectors",
            )
        try:
            selectors = selector_builder(plan)
        except Exception as exc:
            return ReadbackFailure(
                self.site,
                "readback_visibility_selectors_error",
                f"{exc.__class__.__name__}: {exc}",
            )
        if isinstance(selectors, ReadbackFailure):
            return selectors
        if not isinstance(selectors, Mapping) or not selectors:
            return ReadbackFailure(
                self.site,
                "invalid_readback_visibility_selectors",
                "Rocket.Chat selector adapter returned no independent selectors",
            )
        normalized: dict[str, str] = {}
        seen: set[str] = set()
        for raw_key, raw_selector in selectors.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                return ReadbackFailure(
                    self.site,
                    "invalid_readback_visibility_selectors",
                    "Rocket.Chat selector keys must be non-empty text",
                )
            if (
                not isinstance(raw_selector, str)
                or not raw_selector.strip()
                or len(raw_selector.strip()) > 240
                or "\n" in raw_selector
                or "\r" in raw_selector
            ):
                return ReadbackFailure(
                    self.site,
                    "invalid_readback_visibility_selectors",
                    f"Rocket.Chat selector for {raw_key!r} is invalid",
                )
            selector = raw_selector.strip()
            if selector in seen:
                return ReadbackFailure(
                    self.site,
                    "duplicate_readback_visibility_selector",
                    "independent partial corrections must not share a selector",
                )
            seen.add(selector)
            normalized[raw_key.strip()] = selector
        return normalized

    def observe_readback_html(
        self,
        html: str,
        plan: Any,
    ) -> ReadbackObservation | ReadbackFailure:
        adapter = self._readback_adapter
        observer = getattr(adapter, "observe_readback_html", None)
        if not callable(observer):
            return ReadbackFailure(
                self.site,
                "unsupported_readback_observation",
                "Rocket.Chat has no configured exact ordinary-reader HTML observer",
            )
        if not isinstance(html, str) or not html.strip():
            return ReadbackFailure(
                self.site,
                "malformed_readback_html",
                "ordinary-reader readback HTML is empty",
            )
        try:
            observation = observer(html, plan)
        except Exception as exc:
            return ReadbackFailure(
                self.site,
                "readback_observer_error",
                f"{exc.__class__.__name__}: {exc}",
            )
        if not isinstance(observation, (ReadbackObservation, ReadbackFailure)):
            return ReadbackFailure(
                self.site,
                "invalid_readback_observation",
                "Rocket.Chat observer must return a typed readback value",
            )
        return observation

    def interpret_readback(self, observation: ReadbackObservation) -> ReadbackDecision:
        if not isinstance(observation, ReadbackObservation):
            return ReadbackDecision(False, "malformed_observation")
        if observation.kind != "resource_signature":
            return ReadbackDecision(False, "unsupported_readback_kind")
        identity = observation.identity_tokens
        if not isinstance(identity, Mapping):
            return ReadbackDecision(False, "malformed_identity")

        message_keys = _expected_message_keys(identity)
        if message_keys is None:
            return ReadbackDecision(False, "invalid_message_order")
        required = (
            "attempt_id",
            "room_id",
            "room_name",
            "thread_id",
            "writer_user",
            "reader_user_id",
            "reader_auth_context_id",
            *tuple(f"{key}_message_id" for key in message_keys),
            *tuple(f"{key}_body_sha256" for key in message_keys),
        )
        if any(_token(identity, key) is None for key in required):
            return ReadbackDecision(False, "missing_conversation_identity")
        signature = observation.signature.strip() if isinstance(observation.signature, str) else ""
        if not signature:
            return ReadbackDecision(False, "missing_signature")

        payload = observation.payload
        if not isinstance(payload, Mapping):
            return ReadbackDecision(False, "malformed_payload")
        if _payload_token(payload, "room_id", "room") != _token(identity, "room_id"):
            return ReadbackDecision(False, "room_identity_mismatch")
        if _payload_token(payload, "room_name", "channel_name", "channel") != _token(
            identity, "room_name"
        ):
            return ReadbackDecision(False, "room_name_mismatch")
        if _payload_token(payload, "thread_id", "root_message_id") != _token(identity, "thread_id"):
            return ReadbackDecision(False, "thread_identity_mismatch")
        if _payload_token(payload, "reader_user_id", "reader_user") != _token(
            identity, "reader_user_id"
        ):
            return ReadbackDecision(False, "reader_identity_mismatch")
        if _payload_token(payload, "reader_auth_context_id", "reader_auth_context") != _token(
            identity, "reader_auth_context_id"
        ):
            return ReadbackDecision(False, "reader_auth_context_mismatch")
        if not _is_true_marker(payload, "independent_reader", "independent_authenticated_reader"):
            return ReadbackDecision(False, "not_independent_reader")
        if not _is_true_marker(payload, "visible", "visible_to_independent_reader"):
            return ReadbackDecision(False, "conversation_not_visible")
        correction_keys = _correction_keys(message_keys)
        if set(correction_keys) == {"owner_correction", "date_correction"}:
            # Each field correction has its own exact selector and geometry
            # witness.  A global ``painted`` marker is deliberately ignored so
            # one visible row cannot stand in for its sibling.
            for key in correction_keys:
                if not _painted_for_key(payload, key):
                    return ReadbackDecision(False, f"{key}_not_painted")
        elif not _is_true_marker(payload, "painted", "painted_visibility", "painted_at_entry"):
            # Preserve the legacy three-message gate byte-for-byte.
            return ReadbackDecision(False, "conversation_not_painted")

        rows = _messages(payload)
        if rows is None:
            return ReadbackDecision(False, "message_order_or_identity_mismatch")
        actual_keys = tuple(str(row.get("logical_key") or "") for row in rows)
        if actual_keys != message_keys:
            partial_keys = {"owner_correction", "date_correction"}
            if not (
                set(message_keys) == {"plan", "update", *partial_keys}
                and actual_keys[:2] == ("plan", "update")
                and set(actual_keys[2:]) == partial_keys
            ):
                return ReadbackDecision(False, "message_order_or_identity_mismatch")
        seen_ids: set[str] = set()
        rendered_text = ""
        thread_key = _thread_key(identity, message_keys)
        if thread_key is None:
            return ReadbackDecision(False, "thread_identity_mismatch")
        signature_found = False
        for key, row in zip(actual_keys, rows, strict=True):
            expected_id = _token(identity, f"{key}_message_id")
            expected_digest = _token(identity, f"{key}_body_sha256")
            if _payload_token(row, "message_id", "id") != expected_id:
                return ReadbackDecision(False, f"{key}_message_identity_mismatch")
            if expected_id in seen_ids:
                return ReadbackDecision(False, "duplicate_message_identity")
            seen_ids.add(expected_id or "")
            if _payload_token(row, "room_id", "room") != _token(identity, "room_id"):
                return ReadbackDecision(False, f"{key}_room_identity_mismatch")
            expected_author = _token(identity, "writer_user")
            if _payload_token(row, "author", "username", "user") != expected_author:
                return ReadbackDecision(False, f"{key}_author_mismatch")
            body = row.get("body", row.get("text"))
            if not isinstance(body, str) or not body.strip():
                return ReadbackDecision(False, f"{key}_body_missing")
            if (
                expected_digest is None
                or hashlib.sha256(body.encode("utf-8")).hexdigest() != expected_digest
            ):
                return ReadbackDecision(False, f"{key}_body_mismatch")
            if key == thread_key:
                if row.get("thread_id") not in (None, ""):
                    return ReadbackDecision(False, f"{key}_thread_identity_mismatch")
            elif _payload_token(row, "thread_id", "root_message_id") != _token(
                identity, "thread_id"
            ):
                return ReadbackDecision(False, f"{key}_thread_identity_mismatch")
            if key in _correction_keys(message_keys) and signature in body:
                rendered_text = body
                signature_found = True
        if not signature_found:
            return ReadbackDecision(False, "signature_not_in_correction")
        if len(seen_ids) != len(message_keys):
            return ReadbackDecision(False, "duplicate_message_identity")
        return ReadbackDecision(
            True,
            "exact_rocket_chat_conversation_painted",
            matched_signature=signature,
            rendered_text=rendered_text,
        )


def interpret_readback(observation: ReadbackObservation) -> ReadbackDecision:
    """Functional convenience facade for feature-local callers."""

    return RocketChatReadbackCapability().interpret_readback(observation)


__all__ = [
    "RocketChatReadbackAdapter",
    "RocketChatReadbackCapability",
    "interpret_readback",
]
