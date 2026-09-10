"""Measured Rocket.Chat 5.3 thread-panel HTML parser.

The parser extracts exact direct UserMessage rows from the scoped thread
panel. It is deliberately separate from readback interpretation: serialized
HTML establishes DOM identity/body/order only; the render executor must supply
the independent non-zero geometry witness for Painted Visibility.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser

from warp_taskgen.sites.readback import identity_token_text

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_LEGACY_MESSAGE_KEYS = ("plan", "update", "correction")
_PARTIAL_MESSAGE_KEYS = ("plan", "update", "owner_correction", "date_correction")
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def _class_tokens(value: object) -> frozenset[str]:
    if not isinstance(value, str):
        return frozenset()
    return frozenset(part for part in value.split() if part)


def _css_attr_value(value: object) -> str | None:
    """Return an attribute value safe to interpolate into a CSS selector."""

    text = identity_token_text(value)
    if text is None or _ID_RE.fullmatch(text) is None:
        return None
    return text


def _expected_message_keys(identity: object) -> tuple[str, ...] | None:
    """Resolve the exact message order from bounded identity metadata.

    The legacy conversation has one correction.  The partial-update pilot has
    two field-specific corrections and may render those two in either order.
    A writer can preserve an explicit order in ``message_order``; when that
    token is absent the canonical partial order is used as a safe fallback.
    """

    if not isinstance(identity, Mapping):
        return None
    get = identity.get
    raw_order = identity_token_text(get("message_order"))
    ordered: tuple[str, ...] | None = None
    if raw_order is not None:
        parts = tuple(part.strip() for part in raw_order.split(",") if part.strip())
        if not parts or len(parts) != len(set(parts)):
            return None
        ordered = parts

    # The default generator names its root ``plan``, but the public
    # constructor has always allowed a custom thread key.  Derive that one
    # root from the emitted per-message IDs instead of assuming the literal
    # name.  Any extra logical key remains a shape error below.
    message_token_keys = {
        key.removesuffix("_message_id")
        for key, value in identity.items()
        if isinstance(key, str)
        and key.endswith("_message_id")
        and identity_token_text(value) is not None
    }
    message_token_keys.update(
        key.removesuffix("_body_sha256")
        for key, value in identity.items()
        if isinstance(key, str)
        and key.endswith("_body_sha256")
        and identity_token_text(value) is not None
    )
    partial_keys = {"owner_correction", "date_correction"}
    has_partial_marker = bool(message_token_keys.intersection(partial_keys))
    if ordered is not None:
        has_partial_marker = has_partial_marker or bool(partial_keys.intersection(ordered))
        if has_partial_marker:
            # Partial updates have two independently addressed corrections,
            # plus update and exactly one root (which may be custom).
            root_candidates = set(ordered) - partial_keys - {"update"}
            if len(root_candidates) != 1 or "update" not in ordered or len(ordered) != 4:
                return None
            if not partial_keys.issubset(ordered):
                return None
            return ordered
        # Legacy allows a custom root but still has exactly one update and
        # one combined correction message.
        root_candidates = set(ordered) - {"update", "correction"}
        if len(root_candidates) != 1 or "update" not in ordered or "correction" not in ordered:
            return None
        if len(ordered) != 3:
            return None
        return ordered

    if has_partial_marker:
        root_candidates = message_token_keys - partial_keys - {"update"}
        if len(root_candidates) > 1:
            return None
        root = (
            "plan"
            if "plan" in root_candidates or not root_candidates
            else sorted(root_candidates)[0]
        )
        return (root, "update", "owner_correction", "date_correction")

    if (
        not message_token_keys.intersection({"plan", "update", "correction"})
        and len(message_token_keys) != 1
    ):
        return None
    root_candidates = message_token_keys - {"update", "correction"}
    if len(root_candidates) > 1:
        return None
    root = (
        "plan" if "plan" in root_candidates or not root_candidates else sorted(root_candidates)[0]
    )
    return (root, "update", "correction")


@dataclass
class _ThreadPanelRow:
    message_id: str
    thread_id: str | None
    author: str | None
    body_parts: list[str]

    @property
    def body(self) -> str:
        # ``HTMLParser(convert_charrefs=True)`` has already decoded entities.
        # Keep interior spacing intact: the writer's body digest is over the
        # exact string, and the TAC composer emits one-line message bodies.
        return "".join(self.body_parts).strip()


@dataclass
class _ThreadPanelFrame:
    tag: str
    classes: frozenset[str] = frozenset()
    thread_view: bool = False
    thread_list: bool = False
    row: _ThreadPanelRow | None = None
    body_marker: bool = False


class _RocketChatThreadPanelParser(HTMLParser):
    """Extract only direct UserMessage rows from the measured thread panel."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.frames: list[_ThreadPanelFrame] = []
        self.rows: list[_ThreadPanelRow] = []
        self.malformed = False

    @property
    def _inside_thread_view(self) -> bool:
        return any(frame.thread_view for frame in self.frames)

    @property
    def _inside_thread_list(self) -> bool:
        return any(frame.thread_list for frame in self.frames)

    @property
    def _current_row(self) -> _ThreadPanelRow | None:
        for frame in reversed(self.frames):
            if frame.row is not None:
                return frame.row
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.casefold()
        attr_map = {str(key).casefold(): str(value or "") for key, value in attrs}
        classes = _class_tokens(attr_map.get("class"))
        frame = _ThreadPanelFrame(
            tag=tag_name,
            classes=classes,
            thread_view="rcx-thread-view" in classes and not self._inside_thread_view,
            thread_list=(
                tag_name == "ul"
                and "thread" in classes
                and self._inside_thread_view
                and any("js-scroll-thread" in ancestor.classes for ancestor in self.frames)
            ),
        )
        if (
            tag_name == "li"
            and self._inside_thread_list
            and self.frames
            and self.frames[-1].thread_list
            and attr_map.get("data-qa-id", "").casefold() == "usermessage"
        ):
            message_id = _css_attr_value(attr_map.get("data-id"))
            if message_id is None:
                self.malformed = True
            else:
                thread_id = _css_attr_value(attr_map.get("data-tmid"))
                if attr_map.get("data-tmid", "").strip() and thread_id is None:
                    self.malformed = True
                author = attr_map.get("data-username", "").strip() or None
                frame.row = _ThreadPanelRow(message_id, thread_id, author, [])
        if self._current_row is not None:
            frame.body_marker = attr_map.get("data-qa-type") == "message-body"
            if frame.body_marker and not any(parent.body_marker for parent in self.frames):
                # The body marker must be inside the row currently being
                # collected; nested markers are ignored rather than doubled.
                pass
            for key in ("data-username", "data-author"):
                candidate = attr_map.get(key, "").strip()
                if candidate and self._current_row.author is None:
                    self._current_row.author = candidate
        self.frames.append(frame)
        if tag_name in _VOID_TAGS:
            # ``HTMLParser`` does not emit an end tag for void elements.
            self._close_frame(tag_name)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _VOID_TAGS and self.frames:
            self._close_frame(tag.casefold())

    def _close_frame(self, tag_name: str) -> None:
        if not self.frames:
            self.malformed = True
            return
        frame = self.frames.pop()
        if frame.tag != tag_name:
            self.malformed = True
        if frame.row is not None:
            if not frame.row.body:
                self.malformed = True
            self.rows.append(frame.row)

    def handle_endtag(self, tag: str) -> None:
        self._close_frame(tag.casefold())

    def handle_data(self, data: str) -> None:
        row = self._current_row
        if row is None or not data:
            return
        if any(frame.body_marker for frame in self.frames):
            row.body_parts.append(data)
