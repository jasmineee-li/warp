"""Pure Site-owned interpretation of readback observations."""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

ReadbackKind = Literal["resource_identity", "resource_signature", "comment_visibility"]


def _freeze_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_payload(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_payload(item) for item in value)
    if isinstance(value, Set) and not isinstance(value, (str, bytes)):
        return frozenset(_freeze_payload(item) for item in value)
    return value


def identity_token_text(value: Any) -> str | None:
    """Normalize an opaque scalar resource ID without accepting containers."""

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class ReadbackObservation:
    kind: ReadbackKind
    identity_tokens: Mapping[str, Any]
    payload: Any
    signature: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"resource_identity", "resource_signature", "comment_visibility"}:
            raise ValueError("unsupported readback observation kind")
        if not isinstance(self.identity_tokens, Mapping):
            raise ValueError("readback observation requires identity-token metadata")
        if self.signature is not None and not isinstance(self.signature, str):
            raise ValueError("readback observation signature must be text")
        object.__setattr__(self, "identity_tokens", _freeze_payload(self.identity_tokens))
        object.__setattr__(self, "payload", _freeze_payload(self.payload))


@dataclass(frozen=True)
class ReadbackDecision:
    verified: bool
    reason: str
    matched_signature: str | None = None
    rendered_text: str | None = None


@dataclass(frozen=True)
class ReadbackFailure:
    site: str
    reason: str
    detail: str


@runtime_checkable
class ReadbackObservationCapability(Protocol):
    """Optional Site-owned interpretation of an ordinary-reader HTML page.

    The render executor owns the browser context and supplies the resulting
    HTML.  A Site may project that HTML into a typed :class:`ReadbackObservation`
    using its already-bound read-surface plan; no selector language or browser
    behavior is part of this capability.
    """

    def observe_readback_html(
        self,
        html: str,
        plan: Any,
    ) -> ReadbackObservation | ReadbackFailure: ...

    def readback_visibility_selector(self, plan: Any) -> str | ReadbackFailure: ...


class BoundReadback:
    _adapter: Any
    _context: Any

    def supports_readback_observation(self) -> bool:
        """Return whether the bound adapter declares the optional HTML hook."""

        # Feature Sites may expose forwarding methods even when their
        # deployment adapter is intentionally unconfigured.  Consult the
        # concrete capability first; checking method callability alone would
        # turn those forwards into a false admission signal.
        declared = getattr(self._adapter, "supports_readback_observation", None)
        if callable(declared):
            try:
                return bool(declared())
            except Exception:
                return False
        return callable(getattr(self._adapter, "observe_readback_html", None)) and callable(
            getattr(self._adapter, "readback_visibility_selector", None)
        )

    def readback_visibility_selector(self, plan: Any) -> str | ReadbackFailure:
        """Return one Site-owned selector for the exact observed resource."""

        selector_builder = getattr(self._adapter, "readback_visibility_selector", None)
        if not callable(selector_builder):
            return ReadbackFailure(
                self._context.site,
                "unsupported_readback_visibility",
                "Site does not provide exact-resource visibility targeting",
            )
        try:
            selector = selector_builder(plan)
        except Exception as exc:
            return ReadbackFailure(
                self._context.site,
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
                self._context.site,
                "invalid_readback_visibility",
                "Site returned an invalid exact-resource selector",
            )
        return selector.strip()

    def readback_visibility_selectors(self, plan: Any) -> Mapping[str, str] | ReadbackFailure:
        """Return independent selectors for a feature's exact resources.

        The plural seam is intentionally optional.  Existing Sites keep their
        singular selector contract, while a feature such as Rocket.Chat's
        partial-update composition can ask the render executor to probe each
        correction independently.  The executor must never combine these
        selectors into one union query: each mapping entry is one exact
        resource witness.
        """

        selector_builder = getattr(self._adapter, "readback_visibility_selectors", None)
        if not callable(selector_builder):
            return ReadbackFailure(
                self._context.site,
                "unsupported_readback_visibility_selectors",
                "Site does not provide independent exact-resource visibility targeting",
            )
        try:
            selectors = selector_builder(plan)
        except Exception as exc:
            return ReadbackFailure(
                self._context.site,
                "readback_visibility_selectors_error",
                f"{exc.__class__.__name__}: {exc}",
            )
        if isinstance(selectors, ReadbackFailure):
            return selectors
        if not isinstance(selectors, Mapping) or not selectors:
            return ReadbackFailure(
                self._context.site,
                "invalid_readback_visibility_selectors",
                "Site returned no independent exact-resource selectors",
            )
        normalized: dict[str, str] = {}
        seen_selectors: set[str] = set()
        for raw_key, raw_selector in selectors.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                return ReadbackFailure(
                    self._context.site,
                    "invalid_readback_visibility_selectors",
                    "Site selector keys must be non-empty text",
                )
            key = raw_key.strip()
            if (
                not isinstance(raw_selector, str)
                or not raw_selector.strip()
                or len(raw_selector.strip()) > 240
                or "\n" in raw_selector
                or "\r" in raw_selector
            ):
                return ReadbackFailure(
                    self._context.site,
                    "invalid_readback_visibility_selectors",
                    f"Site selector for {key!r} must be bounded single-line text",
                )
            selector = raw_selector.strip()
            if selector in seen_selectors:
                return ReadbackFailure(
                    self._context.site,
                    "duplicate_readback_visibility_selector",
                    "independent resources must not share one selector",
                )
            seen_selectors.add(selector)
            normalized[key] = selector
        return normalized

    def interpret_readback(
        self,
        observation: ReadbackObservation,
    ) -> ReadbackDecision | ReadbackFailure:
        if not isinstance(observation, ReadbackObservation):
            return ReadbackFailure(
                self._context.site,
                "invalid_readback_observation",
                "readback interpretation requires a typed observation",
            )
        interpreter = getattr(self._adapter, "interpret_readback", None)
        if not callable(interpreter):
            return ReadbackFailure(
                self._context.site,
                "unsupported_readback",
                "Site does not provide readback interpretation",
            )
        supported = getattr(self._adapter, "supported_benchmarks", frozenset())
        if self._context.benchmark not in supported:
            return ReadbackFailure(
                self._context.site,
                "unsupported_benchmark",
                f"benchmark {self._context.benchmark!r} is not supported by this Site",
            )
        try:
            decision = interpreter(observation)
        except Exception as exc:
            return ReadbackFailure(
                self._context.site,
                "readback_adapter_error",
                f"{exc.__class__.__name__}: {exc}",
            )
        if not isinstance(decision, ReadbackDecision):
            return ReadbackFailure(
                self._context.site,
                "invalid_readback_decision",
                "Site returned an unsupported readback decision",
            )
        return decision

    def observe_readback_html(
        self,
        html: str,
        plan: Any,
    ) -> ReadbackObservation | ReadbackFailure:
        """Project ordinary-reader HTML through an optional Site capability.

        Existing Sites do not need this hook: the compatibility render path
        continues to use their established readback probes.  A Site that
        declares the hook owns the HTML interpretation and is still checked
        by :meth:`interpret_readback` before render verification can pass.
        """

        observer = getattr(self._adapter, "observe_readback_html", None)
        if not callable(observer):
            return ReadbackFailure(
                self._context.site,
                "unsupported_readback_observation",
                "Site does not provide ordinary-reader HTML observation",
            )
        if not isinstance(html, str):
            return ReadbackFailure(
                self._context.site,
                "malformed_readback_html",
                "ordinary-reader readback HTML must be text",
            )
        supported = getattr(self._adapter, "supported_benchmarks", frozenset())
        if self._context.benchmark not in supported:
            return ReadbackFailure(
                self._context.site,
                "unsupported_benchmark",
                f"benchmark {self._context.benchmark!r} is not supported by this Site",
            )
        try:
            observation = observer(html, plan)
        except Exception as exc:
            return ReadbackFailure(
                self._context.site,
                "readback_observer_error",
                f"{exc.__class__.__name__}: {exc}",
            )
        if not isinstance(observation, (ReadbackObservation, ReadbackFailure)):
            return ReadbackFailure(
                self._context.site,
                "invalid_readback_observation",
                "Site returned an unsupported readback observation value",
            )
        return observation


__all__ = [
    "BoundReadback",
    "ReadbackDecision",
    "ReadbackFailure",
    "ReadbackKind",
    "ReadbackObservation",
    "ReadbackObservationCapability",
    "identity_token_text",
]
