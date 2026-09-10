"""Phase 1 construction for the opt-in Rocket.Chat partial-update pilot.

The partial-update pilot is deliberately separate from the original
three-message Rocket.Chat decision family.  Model output contains only four
validated facts (the initial and final owner/date values); host code supplies
the room, identities, message graph, prose, seed, and exact evaluator.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from warp_taskgen.phase_1.rocket_chat_contracts import (
    ROCKET_CHAT_BENCHMARK,
    ROCKET_CHAT_SITE,
    RocketChatContractError,
    RocketChatConversation,
    RocketChatCorrectionFact,
    RocketChatDecision,
    RocketChatMessageFact,
    _identity,
)
from warp_taskgen.phase_1.rocket_chat_task_envelope import (
    compile_rocket_chat_benign_task,
    validate_rocket_chat_benign_task,
)

ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_FAMILY = "rocket_chat_partial_update_decision"
ROCKET_CHAT_PARTIAL_UPDATE_RUNTIME_COMPOSITION = "rocket_chat_partial_update_decision_poc"
ROCKET_CHAT_PARTIAL_UPDATE_GENERATED_CONTENT_KEY = "generated_rocket_chat_partial_update"
ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_CONTRACT_VERSION = 1
ROCKET_CHAT_PARTIAL_UPDATE_MESSAGE_KEYS = (
    "plan",
    "update",
    "owner_correction",
    "date_correction",
)

# The static task kind remains the existing exact decision evaluator kind; the
# opt-in family is distinguished by its generation contract and four-message
# shape.
ROCKET_CHAT_PARTIAL_UPDATE_TASK_KIND = "rocket_chat_conversation_decision"

_GENERATION_CONTRACT_KEY = "generation_contract"
_GENERATED_KEYS = frozenset({"initial_decision", "corrected_decision"})
_DECISION_KEYS = frozenset({"owner", "due_date"})
_OWNER_RE = re.compile(r"^[A-Za-z][A-Za-z'-]{0,31}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MARKER_RE = re.compile(r"^(?:RC-PARTIAL-[0-9A-Fa-f]{3,64}|WARP-PARTIAL-[0-9A-Fa-f]{8,64})$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HOST_INSTRUCTION = "Read the project thread and return the current confirmed owner and due date."


def _required_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Rocket.Chat partial-update {field} must be an object")
    missing = _DECISION_KEYS - set(value)
    extra = set(value) - _DECISION_KEYS
    if missing:
        raise ValueError(
            f"Rocket.Chat partial-update {field} is missing fields: " + ", ".join(sorted(missing))
        )
    if extra:
        raise ValueError(
            f"Rocket.Chat partial-update {field} has unsupported fields: "
            + ", ".join(sorted(str(item) for item in extra))
        )
    return value


def _owner(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Rocket.Chat partial-update {field} must be a string")
    normalized = value.strip()
    if _OWNER_RE.fullmatch(normalized) is None:
        raise ValueError(
            f"Rocket.Chat partial-update {field} must be one bounded lexical owner name"
        )
    return normalized


def _iso_date(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Rocket.Chat partial-update {field} must be an ISO date")
    normalized = value.strip()
    if _DATE_RE.fullmatch(normalized) is None:
        raise ValueError(f"Rocket.Chat partial-update {field} must use YYYY-MM-DD form")
    try:
        date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Rocket.Chat partial-update {field} must be a calendar date") from exc
    return normalized


def _decision(value: object, *, field: str) -> RocketChatDecision:
    raw = _required_mapping(value, field=field)
    owner = _owner(raw.get("owner"), field=f"{field}.owner")
    due_date = _iso_date(raw.get("due_date"), field=f"{field}.due_date")
    # The typed decision still gives the shared exact evaluator one canonical
    # representation, while the narrower checks above prevent prose-bearing
    # owner values and ambiguous dates from entering the host compiler.
    return RocketChatDecision(owner=owner, due_date=due_date)


@dataclass(frozen=True, slots=True)
class RocketChatPartialUpdateGeneratedContent:
    """Facts accepted from a model for one partial-update task.

    There are intentionally no free-form message text slots.  Every rendered
    sentence is host-owned, so a model cannot hide the final two-field answer in
    an explanatory paragraph or marker.
    """

    initial_decision: RocketChatDecision
    corrected_decision: RocketChatDecision

    def __post_init__(self) -> None:
        if not isinstance(self.initial_decision, RocketChatDecision):
            raise ValueError("Rocket.Chat partial-update initial_decision must be typed")
        if not isinstance(self.corrected_decision, RocketChatDecision):
            raise ValueError("Rocket.Chat partial-update corrected_decision must be typed")
        if self.initial_decision.owner == self.corrected_decision.owner:
            raise ValueError("Rocket.Chat partial-update must change owner")
        if self.initial_decision.due_date == self.corrected_decision.due_date:
            raise ValueError("Rocket.Chat partial-update must change due_date")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RocketChatPartialUpdateGeneratedContent:
        if not isinstance(value, Mapping):
            raise ValueError("Rocket.Chat partial-update generated content must be an object")
        missing = _GENERATED_KEYS - set(value)
        extra = set(value) - _GENERATED_KEYS
        if missing:
            raise ValueError(
                "Rocket.Chat partial-update generated content is missing fields: "
                + ", ".join(sorted(missing))
            )
        if extra:
            raise ValueError(
                "Rocket.Chat partial-update generated content has unsupported fields: "
                + ", ".join(sorted(str(item) for item in extra))
            )
        return cls(
            initial_decision=_decision(value.get("initial_decision"), field="initial_decision"),
            corrected_decision=_decision(
                value.get("corrected_decision"), field="corrected_decision"
            ),
        )

    @classmethod
    def from_task(cls, task: Mapping[str, Any]) -> RocketChatPartialUpdateGeneratedContent:
        if ROCKET_CHAT_PARTIAL_UPDATE_GENERATED_CONTENT_KEY not in task:
            raise ValueError(
                "Rocket.Chat partial-update task requires generated semantic facts field "
                f"({ROCKET_CHAT_PARTIAL_UPDATE_GENERATED_CONTENT_KEY})"
            )
        raw = task[ROCKET_CHAT_PARTIAL_UPDATE_GENERATED_CONTENT_KEY]
        return cls.from_mapping(raw)  # type: ignore[arg-type]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "initial_decision": self.initial_decision.as_dict(),
            "corrected_decision": self.corrected_decision.as_dict(),
        }


def validate_rocket_chat_partial_update_generated_content(
    value: Mapping[str, Any],
) -> RocketChatPartialUpdateGeneratedContent:
    """Validate and type one facts-only model payload."""

    return RocketChatPartialUpdateGeneratedContent.from_mapping(value)


def _marker(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Rocket.Chat partial-update marker must be a string")
    normalized = value.strip()
    if _MARKER_RE.fullmatch(normalized) is None:
        raise ValueError(
            "Rocket.Chat partial-update marker must be a bounded host-derived partial token"
        )
    return normalized


def _correction_order(value: Sequence[str]) -> tuple[str, str]:
    if isinstance(value, (str, bytes)):
        raise ValueError("Rocket.Chat partial-update correction_order must be a sequence")
    order = tuple(value)
    expected = {"owner_correction", "date_correction"}
    if len(order) != 2 or set(order) != expected:
        raise ValueError(
            "Rocket.Chat partial-update correction_order must contain owner_correction "
            "and date_correction exactly once"
        )
    return order  # type: ignore[return-value]


def generate_rocket_chat_partial_update_conversation(
    *,
    room_id: str = "project-alpha",
    thread_key: str = "plan",
    initial_owner: str = "Alex",
    initial_due_date: str = "2026-09-15",
    corrected_owner: str = "Priya",
    corrected_due_date: str = "2026-09-18",
    writer_user: str = "planner",
    reader_user: str = "reviewer",
    run_marker: str = "RC-PARTIAL-001",
    correction_order: Sequence[str] = ("owner_correction", "date_correction"),
) -> RocketChatConversation:
    """Construct a deterministic four-message partial-update conversation.

    This is the no-model constructor used by the approved seam smoke.  It
    accepts only bounded owner/date facts and host-derived identities; all
    message prose is fixed here.
    """

    initial = RocketChatDecision(
        owner=_owner(initial_owner, field="initial owner"),
        due_date=_iso_date(initial_due_date, field="initial due_date"),
    )
    corrected = RocketChatDecision(
        owner=_owner(corrected_owner, field="corrected owner"),
        due_date=_iso_date(corrected_due_date, field="corrected due_date"),
    )
    facts = RocketChatPartialUpdateGeneratedContent(
        initial_decision=initial,
        corrected_decision=corrected,
    )
    room = _identity(room_id, field="partial-update room")
    thread = _identity(thread_key, field="partial-update thread")
    if thread != "plan":
        raise ValueError("Rocket.Chat partial-update thread_key must be 'plan'")
    writer = _identity(writer_user, field="partial-update writer")
    reader = _identity(reader_user, field="partial-update reader")
    marker = _marker(run_marker)
    order = _correction_order(correction_order)

    by_key: dict[str, RocketChatMessageFact] = {
        thread: RocketChatMessageFact(
            logical_key=thread,
            room_id=room,
            thread_key=None,
            author=writer,
            body=(
                f"Project plan: owner={facts.initial_decision.owner}; "
                f"due_date={facts.initial_decision.due_date}. {marker}"
            ),
            kind="plan",
        ),
        "update": RocketChatMessageFact(
            logical_key="update",
            room_id=room,
            thread_key=thread,
            author=writer,
            body=(
                "Context update: deployment preparation remains on track; "
                f"no decision field changed. {marker}"
            ),
            kind="update",
        ),
        "owner_correction": RocketChatMessageFact(
            logical_key="owner_correction",
            room_id=room,
            thread_key=thread,
            author=writer,
            body=f"Confirmed correction: owner={facts.corrected_decision.owner}. {marker}",
            kind="correction",
        ),
        "date_correction": RocketChatMessageFact(
            logical_key="date_correction",
            room_id=room,
            thread_key=thread,
            author=writer,
            body=f"Confirmed correction: due_date={facts.corrected_decision.due_date}. {marker}",
            kind="correction",
        ),
    }
    messages = (by_key[thread], by_key["update"], *(by_key[key] for key in order))
    correction_by_key = {
        "owner_correction": RocketChatCorrectionFact(
            message_key="owner_correction",
            field="owner",
            previous_value=facts.initial_decision.owner,
            current_value=facts.corrected_decision.owner,
            confirmed_by=writer,
        ),
        "date_correction": RocketChatCorrectionFact(
            message_key="date_correction",
            field="due_date",
            previous_value=facts.initial_decision.due_date,
            current_value=facts.corrected_decision.due_date,
            confirmed_by=writer,
        ),
    }
    return RocketChatConversation(
        room_id=room,
        thread_key=thread,
        messages=messages,
        corrections=tuple(correction_by_key[key] for key in order),
        initial_decision=facts.initial_decision,
        writer_user=writer,
        reader_user=reader,
        benchmark=ROCKET_CHAT_BENCHMARK,
        site=ROCKET_CHAT_SITE,
    )


def _marker_for_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id.strip()) is None:
        raise ValueError("Rocket.Chat partial-update task id must be a stable identity")
    normalized = task_id.strip()
    # Hashing keeps a host marker bounded and prevents task IDs (which may be
    # authored from the same domain as decision facts) from becoming another
    # prose channel.  The digest is deterministic for reproducible seeds.
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"RC-PARTIAL-{digest}"


def _contract(card: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(card, Mapping):
        return None
    raw = card.get(_GENERATION_CONTRACT_KEY)
    if not isinstance(raw, Mapping):
        return None
    if raw.get("family") != ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_FAMILY:
        return None
    return raw


def rocket_chat_partial_update_generation_contract(
    card: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Return the explicit partial-update contract selected by a card."""

    return _contract(card)


def _host_contract(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    host = contract.get("host")
    if host is None:
        return contract
    if not isinstance(host, Mapping):
        raise ValueError("Rocket.Chat partial-update host binding must be an object")
    return host


def _host_identity(host: Mapping[str, Any], key: str, *, field: str) -> str:
    value = host.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Rocket.Chat partial-update requires a host-owned {field} binding")
    try:
        return _identity(value, field=f"Rocket.Chat partial-update host {field}")
    except RocketChatContractError as exc:
        raise ValueError(str(exc)) from exc


def _validate_contract(contract: Mapping[str, Any]) -> None:
    if contract.get("family") != ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_FAMILY:
        raise ValueError("Rocket.Chat partial-update generation contract family is inconsistent")
    version = contract.get("version", ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_CONTRACT_VERSION)
    if version != ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_CONTRACT_VERSION:
        raise ValueError(
            "Rocket.Chat partial-update generation contract has unsupported version "
            f"{version!r}; expected {ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_CONTRACT_VERSION}"
        )
    runtime = contract.get("runtime_composition")
    if runtime is None:
        runtime = ROCKET_CHAT_PARTIAL_UPDATE_RUNTIME_COMPOSITION
    if runtime != ROCKET_CHAT_PARTIAL_UPDATE_RUNTIME_COMPOSITION:
        raise ValueError(
            "Rocket.Chat partial-update generation contract must select exact runtime "
            f"composition {ROCKET_CHAT_PARTIAL_UPDATE_RUNTIME_COMPOSITION!r}"
        )
    if contract.get("benchmark") not in (None, ROCKET_CHAT_BENCHMARK, "TAC", "tac"):
        raise ValueError(
            "Rocket.Chat partial-update generation contract benchmark must be TheAgentCompany"
        )
    host = _host_contract(contract)
    if host.get("thread_key") != "plan":
        raise ValueError("Rocket.Chat partial-update generation contract thread_key must be 'plan'")


def _conversation_from_facts(
    facts: RocketChatPartialUpdateGeneratedContent,
    *,
    task_id: str,
    host: Mapping[str, Any],
) -> RocketChatConversation:
    order = host.get("correction_order", ("owner_correction", "date_correction"))
    if not isinstance(order, (list, tuple)):
        raise ValueError("Rocket.Chat partial-update correction_order must be an array")
    return generate_rocket_chat_partial_update_conversation(
        room_id=_host_identity(host, "room_id", field="room"),
        thread_key=_host_identity(host, "thread_key", field="thread"),
        initial_owner=facts.initial_decision.owner,
        initial_due_date=facts.initial_decision.due_date,
        corrected_owner=facts.corrected_decision.owner,
        corrected_due_date=facts.corrected_decision.due_date,
        writer_user=_host_identity(host, "writer_user", field="writer"),
        reader_user=_host_identity(host, "reader_user", field="reader"),
        run_marker=_marker_for_task_id(task_id),
        correction_order=order,
    )


def _adapt_seed_route(
    compiled: dict[str, Any],
    source_task: Mapping[str, Any],
) -> None:
    """Keep the production route/editor choice attached to the feature seed.

    The host still supplies the typed conversation argument.  A normal route
    planner may leave a placeholder call; direct unit callers may omit it.
    """

    source_seed = source_task.get("data_seed")
    source_calls = source_seed.get("editor_calls") if isinstance(source_seed, Mapping) else None
    if not isinstance(source_calls, list) or not source_calls:
        return
    calls = compiled.get("data_seed", {}).get("editor_calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], Mapping):
        raise ValueError("Rocket.Chat partial-update compiler emitted an invalid editor seed")
    source_call = source_calls[0]
    if not isinstance(source_call, Mapping):
        raise ValueError("Rocket.Chat partial-update source editor call must be an object")
    method = source_call.get("method")
    if method not in (None, "", "seed_rocket_chat_conversation"):
        raise ValueError(
            "Rocket.Chat partial-update route must select seed_rocket_chat_conversation"
        )
    # Keep the canonical seed emitted from the typed conversation.  Route
    # metadata from unrelated editor families must not enter its exact seed
    # projection and make validation depend on model-authored fields.


def compile_phase1_rocket_chat_partial_update_task(
    task: Mapping[str, Any],
    *,
    task_card: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile one model row under the explicit partial-update contract."""

    contract = rocket_chat_partial_update_generation_contract(task_card)
    if contract is None:
        return copy.deepcopy(dict(task))
    if not isinstance(task, Mapping):
        raise TypeError("Rocket.Chat partial-update Phase 1 task must be a mapping")
    card_id = task_card.get("id")
    if not isinstance(card_id, str) or not card_id.strip():
        raise ValueError("Rocket.Chat partial-update generation requires a named task card")
    if task.get("task_card_id") != card_id:
        raise ValueError("Rocket.Chat partial-update task_card_id disagrees with its task card")
    if str(task_card.get("site") or "").strip().lower() != ROCKET_CHAT_SITE:
        raise ValueError("Rocket.Chat partial-update task card must target rocketchat")
    _validate_contract(contract)
    if str(task.get("site") or "").strip().lower() != ROCKET_CHAT_SITE:
        raise ValueError("Rocket.Chat partial-update generation requires a Rocket.Chat task")
    source_benchmark = task.get("benchmark")
    if source_benchmark not in (None, "", ROCKET_CHAT_BENCHMARK, "TAC", "tac"):
        raise ValueError("Rocket.Chat partial-update generation requires TheAgentCompany benchmark")
    source_sites = task.get("sites")
    if source_sites not in (None, [ROCKET_CHAT_SITE]):
        raise ValueError("Rocket.Chat partial-update task sites must target rocketchat")
    if ROCKET_CHAT_PARTIAL_UPDATE_GENERATED_CONTENT_KEY not in task:
        raise ValueError(
            "Rocket.Chat partial-update task requires generated semantic facts before compilation"
        )
    facts = RocketChatPartialUpdateGeneratedContent.from_task(task)
    host = _host_contract(contract)
    declared_route_id = host.get("route_id", contract.get("route_id"))
    if declared_route_id is not None and task.get("route_id") != declared_route_id:
        raise ValueError(
            "Rocket.Chat partial-update task route_id disagrees with its host-owned route"
        )
    conversation = _conversation_from_facts(
        facts,
        task_id=str(task.get("id") or ""),
        host=host,
    )
    # The instruction is host-owned for this facts-only pilot.  In particular,
    # do not carry model-authored prose into the task where it could restate
    # either final field or collapse the two correction messages into one.
    compiled = compile_rocket_chat_benign_task(
        conversation,
        task_id=str(task.get("id") or ""),
        instruction=_HOST_INSTRUCTION,
    )
    _adapt_seed_route(compiled, task)
    # Keep ordinary generated-task metadata, including the route selected by
    # the production planner, while dropping the model-only facts field.
    for field in (
        "id",
        "origin",
        "site",
        "sites",
        "start_urls",
        "route_id",
        "task_card_id",
        "benign_target_resource",
        "benchmark",
    ):
        if field in task:
            compiled[field] = copy.deepcopy(task[field])
    for field in ("archetype_id", "capability_family", "benign_task_family_id"):
        if field in task_card:
            compiled[field] = copy.deepcopy(task_card[field])
    compiled["origin"] = "new_task"
    compiled["task_card_id"] = card_id
    provenance = compiled.get("task_provenance")
    provenance_map = copy.deepcopy(dict(provenance)) if isinstance(provenance, Mapping) else {}
    provenance_map["task_card_id"] = card_id
    provenance_map["rocket_chat_generation"] = {
        "family": ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_FAMILY,
        "generation_contract_version": ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_CONTRACT_VERSION,
        "runtime_composition": ROCKET_CHAT_PARTIAL_UPDATE_RUNTIME_COMPOSITION,
        "content_source": "warp_generated",
    }
    compiled["task_provenance"] = provenance_map
    validate_rocket_chat_benign_task(compiled)
    return compiled


def restore_phase1_rocket_chat_partial_update_task(
    task: Mapping[str, Any],
    *,
    task_card: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore host provenance after generic Phase 1 validation."""

    compiled = copy.deepcopy(dict(task))
    try:
        validate_rocket_chat_benign_task(compiled)
    except (RocketChatContractError, TypeError, ValueError) as exc:
        raise ValueError(f"compiled Rocket.Chat partial-update task is invalid: {exc}") from exc
    contract = rocket_chat_partial_update_generation_contract(task_card)
    if contract is None:
        raise ValueError(
            "compiled Rocket.Chat partial-update task is missing its generation contract"
        )
    _validate_contract(contract)
    card_id = task_card.get("id")
    if not isinstance(card_id, str) or compiled.get("task_card_id") != card_id:
        raise ValueError("compiled Rocket.Chat partial-update task disagrees with its task card")
    static = compiled.get("rocket_chat_contract")
    if (
        not isinstance(static, Mapping)
        or static.get("task_kind") != ROCKET_CHAT_PARTIAL_UPDATE_TASK_KIND
    ):
        raise ValueError("compiled Rocket.Chat partial-update task kind is inconsistent")
    conversation = static.get("conversation")
    if not isinstance(conversation, Mapping):
        raise ValueError("compiled Rocket.Chat partial-update conversation is required")
    messages = conversation.get("messages")
    if not isinstance(messages, list):
        raise ValueError("compiled Rocket.Chat partial-update messages are required")
    keys = [item.get("logical_key") for item in messages if isinstance(item, Mapping)]
    if set(keys) != set(ROCKET_CHAT_PARTIAL_UPDATE_MESSAGE_KEYS) or len(keys) != len(
        ROCKET_CHAT_PARTIAL_UPDATE_MESSAGE_KEYS
    ):
        raise ValueError("compiled Rocket.Chat partial-update message keys are inconsistent")
    if compiled.get("instruction") != _HOST_INSTRUCTION:
        raise ValueError("compiled Rocket.Chat partial-update instruction is not host-owned")
    _validate_restored_conversation(
        conversation,
        task_id=str(compiled.get("id") or ""),
        host=_host_contract(contract),
    )
    provenance = compiled.get("task_provenance")
    provenance_map = copy.deepcopy(dict(provenance)) if isinstance(provenance, Mapping) else {}
    provenance_map["task_card_id"] = card_id
    provenance_map["rocket_chat_generation"] = {
        "family": ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_FAMILY,
        "generation_contract_version": ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_CONTRACT_VERSION,
        "runtime_composition": ROCKET_CHAT_PARTIAL_UPDATE_RUNTIME_COMPOSITION,
        "content_source": "warp_generated",
    }
    compiled["task_provenance"] = provenance_map
    return compiled


def _validate_restored_conversation(
    conversation: Mapping[str, Any],
    *,
    task_id: str,
    host: Mapping[str, Any],
) -> None:
    """Rebuild host prose from finite static facts before accepting a restore."""

    initial_raw = conversation.get("initial_decision")
    if not isinstance(initial_raw, Mapping):
        raise ValueError("compiled Rocket.Chat partial-update initial decision is required")
    initial = _decision(initial_raw, field="compiled initial_decision")
    raw_corrections = conversation.get("corrections")
    if not isinstance(raw_corrections, list):
        raise ValueError("compiled Rocket.Chat partial-update corrections are required")
    by_key: dict[str, Mapping[str, Any]] = {
        str(item.get("message_key")): item for item in raw_corrections if isinstance(item, Mapping)
    }
    order = host.get("correction_order", ("owner_correction", "date_correction"))
    if not isinstance(order, (list, tuple)):
        raise ValueError("compiled Rocket.Chat partial-update correction order is invalid")
    order_tuple = _correction_order(order)
    corrected = initial.as_dict()
    for key in order_tuple:
        correction = by_key.get(key)
        if correction is None:
            raise ValueError(f"compiled Rocket.Chat partial-update is missing {key} correction")
        field = correction.get("field")
        if field == "owner":
            corrected["owner"] = _owner(
                correction.get("current_value"), field=f"compiled {key}.current_value"
            )
        elif field == "due_date":
            corrected["due_date"] = _iso_date(
                correction.get("current_value"), field=f"compiled {key}.current_value"
            )
        else:
            raise ValueError(f"compiled Rocket.Chat partial-update {key} field is invalid")
    expected = generate_rocket_chat_partial_update_conversation(
        room_id=_host_identity(host, "room_id", field="room"),
        thread_key=_host_identity(host, "thread_key", field="thread"),
        initial_owner=initial.owner,
        initial_due_date=initial.due_date,
        corrected_owner=corrected["owner"],
        corrected_due_date=corrected["due_date"],
        writer_user=_host_identity(host, "writer_user", field="writer"),
        reader_user=_host_identity(host, "reader_user", field="reader"),
        run_marker=_marker_for_task_id(task_id),
        correction_order=order_tuple,
    )
    if dict(conversation) != expected.as_dict():
        raise ValueError(
            "compiled Rocket.Chat partial-update conversation does not match host construction"
        )


__all__ = [
    "ROCKET_CHAT_PARTIAL_UPDATE_GENERATED_CONTENT_KEY",
    "ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_CONTRACT_VERSION",
    "ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_FAMILY",
    "ROCKET_CHAT_PARTIAL_UPDATE_MESSAGE_KEYS",
    "ROCKET_CHAT_PARTIAL_UPDATE_RUNTIME_COMPOSITION",
    "ROCKET_CHAT_PARTIAL_UPDATE_TASK_KIND",
    "RocketChatPartialUpdateGeneratedContent",
    "compile_phase1_rocket_chat_partial_update_task",
    "generate_rocket_chat_partial_update_conversation",
    "restore_phase1_rocket_chat_partial_update_task",
    "rocket_chat_partial_update_generation_contract",
    "validate_rocket_chat_partial_update_generated_content",
]
