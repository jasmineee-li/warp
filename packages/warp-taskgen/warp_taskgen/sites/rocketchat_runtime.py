"""Feature-local, opt-in Rocket.Chat runtime seams for TAC decision transfer."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urljoin

import requests

from warp_taskgen.phase_1.rocket_chat_contracts import (
    ROCKET_CHAT_BENCHMARK,
    ROCKET_CHAT_SEED_METHOD,
    ROCKET_CHAT_SITE,
    RocketChatContractError,
    RocketChatObservation,
    RocketChatObservationFailure,
)
from warp_taskgen.seeding.site_contracts import EditorSeedResult
from warp_taskgen.sites.read_surface import (
    ReadSurfacePlanFailure,
    ReadSurfaceVerificationPlan,
    build_read_surface_plan,
)
from warp_taskgen.sites.readback import ReadbackFailure
from warp_taskgen.sites.rocketchat import RocketChatSite
from warp_taskgen.sites.rocketchat_browser_auth import (
    RocketChatReaderPreflight,
    preflight_rocket_chat_reader,
)
from warp_taskgen.sites.rocketchat_readback import (
    RocketChatReadbackAdapter,
    RocketChatReadbackCapability,
)
from warp_taskgen.sites.rocketchat_reader import RocketChatHttpReader
from warp_taskgen.sites.rocketchat_reset import (
    RocketChatResetter,
    resetter_from_instance,
)
from warp_taskgen.sites.rocketchat_thread_panel import _expected_message_keys
from warp_taskgen.sites.rocketchat_transport import (
    RequestsRocketChatTransport,
    RocketChatAuthSession,
    RocketChatCredentials,
    RocketChatHttpWriter,
    RocketChatTransport,
    RocketChatTransportError,
    _context,
    _credentials,
    _origin,
)

TAC_SOURCE_COMMIT = "98b68ef82a47690c316f42fddb05baafaab56851"
ROCKET_CHAT_RUNTIME_COMPOSITION = "rocket_chat_conversation_decision_poc"
ROCKET_CHAT_EDITOR_METHOD = ROCKET_CHAT_SEED_METHOD

__all__ = [
    "RequestsRocketChatTransport",
    "RocketChatAuthSession",
    "RocketChatCredentials",
    "RocketChatFeasibilityPolicy",
    "RocketChatHttpEditor",
    "RocketChatHttpReader",
    "RocketChatHttpWriter",
    "RocketChatReaderPreflight",
    "RocketChatRuntimeSite",
    "RocketChatTransport",
    "RocketChatTransportError",
    "_context",
    "preflight_rocket_chat_reader",
    "rocket_chat_credentials",
]


class RocketChatRuntimeSite(RocketChatSite, RocketChatReadbackCapability):
    """Executable TAC Site wiring with an opt-in painted-readback adapter.

    The default constructor deliberately has no browser selector/observer.
    A deployment-specific adapter can be injected once the TAC DOM facts are
    recorded; until then the compatibility body-text plan remains visible but
    Phase 2 admission cannot pass it as exact Painted Visibility evidence.
    """

    def __init__(
        self,
        *,
        readback_adapter: RocketChatReadbackAdapter | None = None,
    ) -> None:
        RocketChatReadbackCapability.__init__(self, readback_adapter)

    def build_read_surface_plan(
        self, *, seed_result: EditorSeedResult, signature: str, origin: str
    ) -> ReadSurfaceVerificationPlan | ReadSurfacePlanFailure:
        common_required = (
            "room_id",
            "room_name",
            "thread_id",
            "writer_user",
            "reader_user_id",
            "reader_auth_context_id",
        )
        message_keys = _expected_message_keys(seed_result.write_tokens)
        if message_keys is None:
            return ReadSurfacePlanFailure(
                "rocketchat",
                "invalid_message_identity",
                "Rocket.Chat readback requires one legacy or partial-update message shape",
            )
        message_identity = tuple(
            item for key in message_keys for item in (f"{key}_message_id", f"{key}_body_sha256")
        )
        required = (*common_required, *message_identity)
        missing = [key for key in required if seed_result.write_tokens.get(key) in (None, "")]
        if missing:
            return ReadSurfacePlanFailure(
                "rocketchat",
                "missing_message_identity",
                "Rocket.Chat readback requires " + ", ".join(missing),
            )
        optional_identity = ()
        if set(message_keys) == {"plan", "update", "owner_correction", "date_correction"}:
            # The partial-update host may render the two corrections in either
            # order; preserve that fact as one bounded token so the ordinary
            # reader and render probe bind the same exact rows.
            if seed_result.write_tokens.get("message_order") not in (None, ""):
                optional_identity = ("message_order",)
        elif seed_result.write_tokens.get("thread_key") not in (None, ""):
            # Legacy custom thread keys are supported without changing the
            # default three-message token shape.
            optional_identity = ("thread_key",)
        plan = build_read_surface_plan(
            site="rocketchat",
            seed_result=seed_result,
            signature=signature,
            origin=origin,
            identity_keys=(
                "attempt_id",
                *required,
                *optional_identity,
            ),
        )
        if isinstance(plan, ReadSurfaceVerificationPlan):
            if self.supports_readback_observation():
                # A configured adapter owns exact message projection.  The
                # generic renderer separately proves that its selector has
                # non-zero painted geometry before invoking the observer.
                return replace(
                    plan,
                    verification_mode="seed_resource",
                    persist_readback_identity_tokens=True,
                )
            # IDs document REST only; no DOM observer means body text, not
            # exact painted identity.  This source-only fallback is never an
            # admission path for TAC.
            return replace(plan, verification_mode="body_text")
        return plan

    # The optional readback capability is composed explicitly instead of
    # registering a global Site adapter.  Delegate to the feature-local owner
    # initialized above so BoundSite can expose the same contract.
    def supports_readback_observation(self) -> bool:
        return RocketChatReadbackCapability.supports_readback_observation(self)

    def readback_visibility_selector(self, plan: Any) -> str | ReadbackFailure:
        return RocketChatReadbackCapability.readback_visibility_selector(self, plan)

    def readback_visibility_selectors(self, plan: Any) -> Mapping[str, str] | ReadbackFailure:
        return RocketChatReadbackCapability.readback_visibility_selectors(self, plan)

    def observe_readback_html(
        self,
        html: str,
        plan: Any,
    ) -> Any:
        return RocketChatReadbackCapability.observe_readback_html(self, html, plan)

    def interpret_readback(self, observation: Any) -> Any:
        return RocketChatReadbackCapability.interpret_readback(self, observation)


@dataclass(frozen=True)
class RocketChatFeasibilityPolicy:
    benchmark: str = ROCKET_CHAT_BENCHMARK
    site: str = ROCKET_CHAT_SITE

    def auth_self_test_path(self) -> str | None:
        return "/api/v1/me"

    def requires_authenticated_preflight(self) -> bool:
        return True

    def probe_targets(self, task: dict[str, Any], instance_site_url: str) -> list[Any]:
        from warp_taskgen.phase_2.phase_2c.policy import ProbeTarget

        starts = task.get("start_urls")
        if isinstance(starts, list):
            return [
                ProbeTarget(
                    url=urljoin(instance_site_url.rstrip("/") + "/", value), source="start_url"
                )
                for value in starts
                if isinstance(value, str) and value.startswith("/")
            ]
        conversation = task.get("conversation")
        room_id = conversation.get("room_id") if isinstance(conversation, Mapping) else None
        return (
            [
                ProbeTarget(
                    url=urljoin(instance_site_url.rstrip("/") + "/", f"/channel/{room_id}"),
                    source="conversation.room_id",
                )
            ]
            if isinstance(room_id, str) and room_id
            else []
        )

    def classify_probe(
        self,
        *,
        status: int | None,
        headers: dict[str, str] | None,
        body_snippet: str,
        exception_name: str | None,
    ) -> Any:
        from warp_taskgen.phase_2.phase_2c.policy import PreflightClassification

        del headers, body_snippet
        if exception_name:
            return PreflightClassification(
                "host_unreachable", False, status, f"Rocket.Chat probe raised {exception_name}"
            )
        if status in {401, 403}:
            return PreflightClassification(
                "auth_missing", True, status, f"Rocket.Chat probe returned HTTP {status}"
            )
        if status == 404:
            return PreflightClassification(
                "not_found", True, status, "Rocket.Chat room was not found"
            )
        if status is not None and 200 <= status < 300:
            return PreflightClassification(
                "reachable", False, status, "Rocket.Chat probe reachable"
            )
        return PreflightClassification(
            "unexpected_status", False, status, f"Rocket.Chat probe returned HTTP {status}"
        )

    def decide_source_data(
        self,
        *,
        task: dict[str, Any],
        classifications_by_target: dict[int, list[Any]],
        target_audit: dict[int, Any],
        candidate_replica_count: int,
        login_redirect_count: int,
        probed_count: int,
        bailout_ratio: float,
    ) -> Any:
        from warp_taskgen.phase_2.phase_2c.policy import SourceDataDecision

        del task, candidate_replica_count, login_redirect_count, probed_count, bailout_ratio
        for index, classifications in classifications_by_target.items():
            for classification in classifications:
                if classification.quarantine:
                    return SourceDataDecision(
                        "drop", classification=classification, target=target_audit[index]
                    )
        return SourceDataDecision("keep")

    def counts_toward_run_bailout(self, classification: Any) -> bool:
        return classification.kind == "auth_missing"

    def should_bailout_source_data_run(
        self, *, bailout_count: int, probed_count: int, bailout_ratio: float
    ) -> bool:
        return bool(probed_count) and bailout_count / probed_count > bailout_ratio

    def restore_drop_on_run_bailout(self, issue: dict[str, Any]) -> bool:
        return issue.get("kind") == "auth_missing"


class RocketChatHttpEditor:
    site_name = ROCKET_CHAT_SITE
    supported_methods = frozenset({ROCKET_CHAT_EDITOR_METHOD})

    def __init__(
        self,
        instance: dict[str, Any],
        session: requests.Session,
        *,
        transport: RocketChatTransport | None = None,
        reader_transport: RocketChatTransport | None = None,
        resetter: RocketChatResetter | None = None,
    ) -> None:
        self.instance = dict(instance)
        self.session = session
        self.transport = transport or RequestsRocketChatTransport(_origin(instance), session)
        self.reader_transport = reader_transport or RequestsRocketChatTransport(
            _origin(instance), requests.Session()
        )
        self.resetter = resetter if resetter is not None else resetter_from_instance(self.instance)
        self._reader_closed = False
        self._mutation_possible = False
        self._reset_completed = False

    @classmethod
    def probe_base_state(cls, instance: dict[str, Any]) -> None:
        RequestsRocketChatTransport(_origin(instance)).login(_credentials(instance, "writer"))

    def probe_authenticated(self) -> bool:
        try:
            self.transport.login(_credentials(self.instance, "writer"))
            return True
        except (RocketChatContractError, RocketChatTransportError):
            return False

    def validate_args(self, method_name: str, args: dict[str, Any]) -> None:
        if method_name != ROCKET_CHAT_EDITOR_METHOD:
            raise RuntimeError(f"unsupported Rocket.Chat editor method {method_name!r}")
        if not isinstance(args.get("conversation"), Mapping):
            raise ValueError("Rocket.Chat seed conversation must be a mapping")

    def preview_context(self, method_name: str, args: dict[str, Any]) -> dict[str, Any]:
        del method_name, args
        return {}

    def seed_rocket_chat_conversation(self, *, conversation: Mapping[str, Any]) -> dict[str, Any]:
        from warp_taskgen.phase_1.rocket_chat_contracts import RocketChatDecision
        from warp_taskgen.phase_1.rocket_chat_decisions import _validate_conversation

        expected = (
            conversation.get("expected_decision") if isinstance(conversation, Mapping) else None
        )
        if not isinstance(expected, Mapping):
            raise ValueError("Rocket.Chat seed conversation expected_decision is required")
        typed = _validate_conversation(conversation, RocketChatDecision.from_mapping(expected))
        # Arm strict cleanup before the first writer POST, which may mutate before returning.
        self._mutation_possible = True
        receipt = RocketChatHttpWriter(self.instance, transport=self.transport).seed_conversation(
            typed
        )
        observation = RocketChatHttpReader(self.instance, transport=self.reader_transport).observe(
            typed, receipt
        )
        if isinstance(observation, RocketChatObservationFailure):
            self._close_reader_transport()
            raise RocketChatTransportError(
                f"independent reader observation failed: {observation.reason}: {observation.detail}"
            )
        if not isinstance(observation, RocketChatObservation):
            self._close_reader_transport()
            raise RocketChatTransportError(
                "independent reader observation returned an unsupported result"
            )
        reader_preflight = preflight_rocket_chat_reader(self.instance)
        if not reader_preflight.ok or reader_preflight.reader_user_id is None:
            self._close_reader_transport()
            raise RocketChatTransportError(
                "independent reader browser identity is unavailable: "
                f"{reader_preflight.reason or 'missing_reader_identity'}"
            )
        if observation.reader_context.user_id != reader_preflight.reader_user_id:
            self._close_reader_transport()
            raise RocketChatTransportError(
                "independent reader REST identity does not match the browser reader identity"
            )
        root_identity = receipt.messages.get(typed.thread_key)
        if (
            root_identity is None
            or observation.room_id != root_identity.room_id
            or observation.thread_id != root_identity.message_id
        ):
            self._close_reader_transport()
            raise RocketChatTransportError(
                "independent reader thread identity disagreed with the writer receipt"
            )
        typed_keys = tuple(message.logical_key for message in typed.messages)
        if set(observation.messages) != set(typed_keys) or set(receipt.messages) != set(typed_keys):
            self._close_reader_transport()
            raise RocketChatTransportError(
                "independent reader did not return exactly the seeded logical message keys"
            )
        for key in typed_keys:
            receipt_identity = receipt.messages.get(key)
            observed_identity = observation.messages.get(key)
            if receipt_identity is None or observed_identity is None:
                self._close_reader_transport()
                raise RocketChatTransportError(
                    f"independent reader omitted seeded {key} message identity"
                )
            if (
                observed_identity.message_id != receipt_identity.message_id
                or observed_identity.room_id != receipt_identity.room_id
                or observed_identity.thread_id != receipt_identity.thread_id
                or observed_identity.author != receipt_identity.author
                or observed_identity.body != receipt_identity.body
            ):
                self._close_reader_transport()
                raise RocketChatTransportError(
                    f"independent reader disagreed with writer receipt for {key}"
                )
        tokens: dict[str, str] = {
            "attempt_id": receipt.attempt_id,
            "room_id": observation.room_id,
            "room_name": typed.room_id,
            "thread_id": observation.thread_id,
            "writer_user": typed.writer_user,
            "reader_user_id": observation.reader_context.user_id,
            "reader_auth_context_id": observation.reader_context.auth_context_id,
        }
        for key in typed_keys:
            identity = observation.messages[key]
            tokens[f"{key}_message_id"] = identity.message_id
            tokens[f"{key}_body_sha256"] = hashlib.sha256(identity.body.encode()).hexdigest()
        partial_keys = {"owner_correction", "date_correction"}
        if partial_keys.issubset(set(typed_keys)):
            tokens["message_order"] = ",".join(typed_keys)
        elif typed.thread_key != "plan":
            tokens["thread_key"] = typed.thread_key
        # Rocket.Chat's measured thread panel is reached by the deep thread
        # route.  Returning it directly lets the fresh browser reader render
        # the exact root/reply rows without relying on a scripted click.
        thread_url = f"/channel/{typed.room_id}/thread/{observation.thread_id}"
        return {
            "identity_tokens": tokens,
            "read_surface_urls": [thread_url],
            "read_surface_provenance_source": "editor_api_response",
            "created_resource": {
                "url": thread_url,
                "kind": "message",
                "id": observation.thread_id,
            },
        }

    def cleanup(self) -> None:
        close_error: BaseException | None = None
        try:
            self._close_reader_transport()
        except BaseException as exc:  # cleanup must still attempt host reset
            close_error = exc
        reset_error: BaseException | None = None
        if self._mutation_possible and not self._reset_completed:
            if self.resetter is None:
                reset_error = RuntimeError(
                    "Rocket.Chat cleanup requires an explicit disposable TAC reset/admin seam; "
                    "ordinary writer credentials are not used for reset"
                )
            else:
                try:
                    # This is intentionally synchronous: the caller must not
                    # begin another task until the host owner reports success.
                    self.resetter.reset()
                except BaseException as exc:
                    reset_error = exc
                else:
                    self._reset_completed = True
        errors = [error for error in (close_error, reset_error) if error is not None]
        if errors:
            details = "; ".join(f"{type(error).__name__}: {error}" for error in errors)
            raise RuntimeError(f"Rocket.Chat cleanup failed: {details}") from errors[0]

    def _close_reader_transport(self) -> None:
        if self._reader_closed:
            return
        close = getattr(self.reader_transport, "close", None)
        if not callable(close):
            close = getattr(getattr(self.reader_transport, "session", None), "close", None)
        if callable(close):
            close()
        self._reader_closed = True


def rocket_chat_credentials(instance: Mapping[str, Any], *, role: str) -> RocketChatCredentials:
    return _credentials(instance, role)
