"""Typed Rocket.Chat seed construction and post-fill validation."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from warp_taskgen.phase_1.rocket_chat_contracts import RocketChatDecision
from warp_taskgen.phase_1.rocket_chat_decisions import _validate_conversation
from warp_taskgen.phase_2.rocket_chat_common import (
    ROCKET_CHAT_BENCHMARK,
    ROCKET_CHAT_DELIVERY_METHOD,
    ROCKET_CHAT_SITE,
    composition_is_partial_update,
    composition_supports_rocket_chat,
)
from warp_taskgen.phase_2.text_fill.constants import PAYLOAD_PLACEHOLDER
from warp_taskgen.phase_2.text_fill.seed import (
    materialize_adversarial_seed,
    validate_seed_template_contract,
)
from warp_taskgen.runtime_composition import RuntimeComposition

ROCKET_CHAT_LEGACY_MESSAGE_KEYS = ("plan", "update", "correction")
ROCKET_CHAT_PARTIAL_MESSAGE_KEYS = (
    "plan",
    "update",
    "owner_correction",
    "date_correction",
)
ROCKET_CHAT_PARTIAL_CORRECTION_KEYS = frozenset({"owner_correction", "date_correction"})
ROCKET_CHAT_PARTIAL_DEFAULT_PAYLOAD_CARRIER = "date_correction"


def materialize_seed_template(
    contract: Mapping[str, Any],
    *,
    runtime_composition: RuntimeComposition | None,
) -> dict[str, Any]:
    """Build one editor call whose typed conversation has one body placeholder."""

    if (
        not isinstance(contract, Mapping)
        or contract.get("eligibility", {}).get("status") != "eligible"
    ):
        raise ValueError("cannot materialize seed_template from ineligible Rocket.Chat contract")
    if not composition_supports_rocket_chat(
        runtime_composition,
        benchmark=ROCKET_CHAT_BENCHMARK,
        site=ROCKET_CHAT_SITE,
    ):
        raise ValueError(
            "Rocket.Chat seed materialization requires an explicit runtime composition"
        )
    args = contract.get("editor_args_template")
    if not isinstance(args, Mapping) or set(args) != {"conversation"}:
        raise ValueError("Rocket.Chat exposure contract must carry one conversation argument")
    conversation = contract.get("conversation")
    if not isinstance(conversation, Mapping):
        raise ValueError("Rocket.Chat exposure contract is missing its typed conversation")
    shape = _message_shape(conversation.get("messages"))
    if shape is None:
        raise ValueError("Rocket.Chat exposure contract has unsupported message shape")
    if _is_partial_message_shape(shape) != composition_is_partial_update(runtime_composition):
        raise ValueError(
            "Rocket.Chat seed shape is incompatible with the selected runtime composition"
        )
    payload_carrier = _payload_carrier_for_contract(contract, conversation)
    seeded_conversation = _conversation_with_placeholder(
        copy.deepcopy(dict(conversation)),
        payload_carrier=payload_carrier,
    )
    seed = {
        "mechanism": "editor",
        "render_signature": _correction_body(
            conversation,
            payload_carrier=payload_carrier,
        ),
        "editor_calls": [
            {
                "benchmark": ROCKET_CHAT_BENCHMARK,
                "site": ROCKET_CHAT_SITE,
                "method": ROCKET_CHAT_DELIVERY_METHOD,
                "args": {"conversation": seeded_conversation},
            }
        ],
    }
    if _is_partial_message_shape(shape):
        # These fields are host-owned metadata consumed by the exact reader;
        # they make all four logical identities explicit before any write.
        seed["payload_carrier"] = payload_carrier
        seed["message_logical_keys"] = list(shape)
    validate_rocket_chat_seed_template(
        seed,
        benign_conversation=conversation,
        payload_carrier=payload_carrier,
    )
    return seed


def validate_rocket_chat_seed_template(
    seed_template: Mapping[str, Any],
    *,
    benign_conversation: Mapping[str, Any] | None = None,
    payload_carrier: str | None = None,
) -> None:
    """Prove the sole transformation is one selected correction-body append.

    Legacy three-message conversations use ``correction``.  The separately
    gated partial-update pilot uses four rows and appends to one host-selected
    field correction (``date_correction`` by default).  In both cases every
    other structured fact and body must remain byte-for-byte unchanged.
    """

    if not isinstance(seed_template, Mapping):
        raise ValueError("Rocket.Chat seed_template must be an object")
    validate_seed_template_contract(dict(seed_template))
    if seed_template.get("mechanism") != "editor":
        raise ValueError("Rocket.Chat seed_template mechanism must be editor")
    calls = seed_template.get("editor_calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], Mapping):
        raise ValueError("Rocket.Chat seed_template must contain exactly one editor call")
    call = calls[0]
    if (
        call.get("benchmark") != ROCKET_CHAT_BENCHMARK
        or call.get("site") != ROCKET_CHAT_SITE
        or call.get("method") != ROCKET_CHAT_DELIVERY_METHOD
    ):
        raise ValueError("Rocket.Chat seed_template editor identity is inconsistent")
    args = call.get("args")
    conversation = args.get("conversation") if isinstance(args, Mapping) else None
    if not isinstance(args, Mapping) or set(args) != {"conversation"}:
        raise ValueError("Rocket.Chat seed_template editor args must be conversation-only")
    if not isinstance(conversation, Mapping) or _count_placeholder(conversation) != 1:
        raise ValueError("Rocket.Chat conversation must contain exactly one payload placeholder")
    messages = conversation.get("messages")
    shape = _message_shape(messages)
    if shape is None:
        raise ValueError(
            "Rocket.Chat conversation must contain either plan/update/correction or "
            "plan/update/owner_correction/date_correction messages"
        )
    selected = _payload_carrier_for_seed(
        seed_template,
        conversation,
        explicit=payload_carrier,
    )
    if selected not in _correction_keys_for_shape(shape):
        raise ValueError(
            f"Rocket.Chat payload carrier {selected!r} must identify a correction message"
        )
    selected_message = _message_by_key(messages, selected)
    if not isinstance(selected_message, Mapping) or not isinstance(
        selected_message.get("body"), str
    ):
        raise ValueError(f"Rocket.Chat {selected} message is required")
    selected_body = selected_message["body"]
    if not selected_body.endswith(f" {PAYLOAD_PLACEHOLDER}"):
        raise ValueError(f"Rocket.Chat payload must append to the {selected} body")
    if _count_placeholder(selected_body) != 1:
        raise ValueError(f"Rocket.Chat {selected} body must contain one payload placeholder")
    if _count_placeholder([message for message in messages if message is not selected_message]):
        raise ValueError("Rocket.Chat payload placeholder must stay in the selected body")
    declared_keys = seed_template.get("message_logical_keys")
    if _is_partial_message_shape(shape) and declared_keys not in (None, list(shape)):
        raise ValueError("Rocket.Chat partial seed message_logical_keys are inconsistent")
    if benign_conversation is None:
        return
    expected = copy.deepcopy(dict(benign_conversation))
    expected_messages = expected.get("messages")
    expected_shape = _message_shape(expected_messages)
    if expected_shape != shape:
        raise ValueError("Rocket.Chat seed message shape changed from benign conversation")
    expected_selected = _payload_carrier_for_seed(
        seed_template,
        expected,
        explicit=payload_carrier,
    )
    if expected_selected != selected:
        raise ValueError("Rocket.Chat payload carrier changed from benign conversation")
    expected_correction = _message_by_key(expected_messages, selected)
    if not isinstance(expected_correction, Mapping) or not isinstance(
        expected_correction.get("body"), str
    ):
        raise ValueError(f"Rocket.Chat benign {selected} body is required")
    actual = copy.deepcopy(dict(conversation))
    actual_messages = actual["messages"]
    actual_correction = _message_by_key(actual_messages, selected)
    if not isinstance(actual_correction, dict):
        raise ValueError(f"Rocket.Chat selected {selected} message is required")
    actual_correction["body"] = actual_correction["body"][: -len(f" {PAYLOAD_PLACEHOLDER}")]
    if actual != expected:
        raise ValueError(
            "Rocket.Chat seed must preserve every typed conversation fact except for the "
            "selected correction-body payload append"
        )


def materialize_adversarial_seed_for_runtime(
    seed_template: Mapping[str, Any],
    rendered_payload: str,
    *,
    runtime_composition: RuntimeComposition | None,
) -> dict[str, Any]:
    """Replace the placeholder and ensure the resulting call remains typed."""

    if not isinstance(rendered_payload, str) or not rendered_payload.strip():
        raise ValueError("Rocket.Chat rendered payload must be non-empty text")
    if "\n" in rendered_payload or "\r" in rendered_payload:
        raise ValueError("Rocket.Chat rendered payload must be single-line text")
    if not composition_supports_rocket_chat(
        runtime_composition,
        benchmark=ROCKET_CHAT_BENCHMARK,
        site=ROCKET_CHAT_SITE,
    ):
        raise ValueError(
            "Rocket.Chat seed materialization requires an explicit runtime composition"
        )
    validate_rocket_chat_seed_template(seed_template)
    materialized = materialize_adversarial_seed(dict(seed_template), rendered_payload)
    calls = materialized.get("editor_calls")
    conversation = calls[0].get("args", {}).get("conversation") if calls else None
    if not isinstance(conversation, Mapping) or _count_placeholder(conversation):
        raise ValueError("Rocket.Chat materialized seed retained no typed placeholder")
    shape = _message_shape(conversation.get("messages"))
    if shape is None or _is_partial_message_shape(shape) != composition_is_partial_update(
        runtime_composition
    ):
        raise ValueError(
            "Rocket.Chat seed shape is incompatible with the selected runtime composition"
        )
    # The typed envelope validator catches duplicate keys, wrong hosts, and
    # malformed message fields after generic text replacement.
    expected = conversation.get("expected_decision")
    if not isinstance(expected, Mapping):
        raise ValueError("Rocket.Chat materialized conversation expected_decision is required")
    _validate_conversation(conversation, RocketChatDecision.from_mapping(expected))
    return materialized


def _conversation_with_placeholder(
    conversation: dict[str, Any],
    *,
    payload_carrier: str | None = None,
) -> dict[str, Any]:
    messages = conversation.get("messages")
    shape = _message_shape(messages)
    if shape is None:
        raise ValueError("Rocket.Chat conversation message shape is unsupported")
    selected = _payload_carrier_for_conversation(conversation, payload_carrier)
    correction = _message_by_key(messages, selected)
    if not isinstance(correction, dict) or not isinstance(correction.get("body"), str):
        raise ValueError(f"Rocket.Chat {selected} message is required")
    body = correction["body"]
    if body.endswith(PAYLOAD_PLACEHOLDER) or len(body) + len(PAYLOAD_PLACEHOLDER) + 1 > 2000:
        raise ValueError(f"Rocket.Chat {selected} body cannot carry one payload placeholder")
    correction["body"] = f"{body} {PAYLOAD_PLACEHOLDER}"
    return conversation


def _correction_body(conversation: object, *, payload_carrier: str | None = None) -> str:
    if not isinstance(conversation, Mapping):
        raise ValueError("Rocket.Chat exposure contract is missing its typed conversation")
    messages = conversation.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Rocket.Chat exposure contract is missing conversation messages")
    shape = _message_shape(messages)
    if shape is None:
        raise ValueError("Rocket.Chat exposure contract has unsupported message shape")
    selected = _payload_carrier_for_conversation(conversation, payload_carrier)
    message = _message_by_key(messages, selected)
    body = message.get("body") if isinstance(message, Mapping) else None
    if isinstance(body, str) and body.strip():
        return body
    raise ValueError(f"Rocket.Chat exposure contract is missing {selected} body")


def _is_partial_conversation(conversation: object) -> bool:
    messages = conversation.get("messages") if isinstance(conversation, Mapping) else None
    return _is_partial_message_shape(_message_shape(messages))


def _message_shape(messages: object) -> tuple[str, ...] | None:
    if (
        not isinstance(messages, list)
        or not messages
        or any(not isinstance(message, Mapping) for message in messages)
    ):
        return None
    keys = tuple(str(message.get("logical_key") or "") for message in messages)
    if len(keys) != len(set(keys)):
        return None
    if keys == ROCKET_CHAT_LEGACY_MESSAGE_KEYS:
        return keys
    # The partial pilot keeps plan/update first, while the two independent
    # correction rows may be emitted in either order.  Their identity and
    # field provenance remain explicit in the returned physical order.
    if _is_partial_message_shape(keys):
        return keys
    return None


def _is_partial_message_shape(shape: tuple[str, ...] | None) -> bool:
    return bool(
        shape is not None
        and len(shape) == len(ROCKET_CHAT_PARTIAL_MESSAGE_KEYS)
        and shape[:2] == ROCKET_CHAT_PARTIAL_MESSAGE_KEYS[:2]
        and set(shape[2:]) == ROCKET_CHAT_PARTIAL_CORRECTION_KEYS
    )


def _message_by_key(messages: object, key: str) -> Mapping[str, Any] | None:
    if not isinstance(messages, list):
        return None
    for message in messages:
        if isinstance(message, Mapping) and message.get("logical_key") == key:
            return message
    return None


def _correction_keys_for_shape(shape: tuple[str, ...]) -> frozenset[str]:
    return (
        ROCKET_CHAT_PARTIAL_CORRECTION_KEYS
        if _is_partial_message_shape(shape)
        else frozenset({"correction"})
    )


def _payload_carrier_for_conversation(
    conversation: object,
    explicit: str | None = None,
) -> str:
    messages = conversation.get("messages") if isinstance(conversation, Mapping) else None
    shape = _message_shape(messages)
    if shape is None:
        raise ValueError("Rocket.Chat conversation message shape is unsupported")
    candidate = explicit
    if candidate is None and isinstance(conversation, Mapping):
        for field in (
            "payload_carrier",
            "payload_message_key",
            "carrier_message_key",
        ):
            value = conversation.get(field)
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
                break
    if candidate is None:
        candidate = (
            ROCKET_CHAT_PARTIAL_DEFAULT_PAYLOAD_CARRIER
            if _is_partial_message_shape(shape)
            else "correction"
        )
    candidate = str(candidate).strip()
    if candidate not in _correction_keys_for_shape(shape):
        raise ValueError(f"Rocket.Chat payload carrier {candidate!r} is not a correction message")
    return candidate


def _payload_carrier_for_contract(
    contract: Mapping[str, Any],
    conversation: object,
) -> str:
    for field in (
        "payload_carrier",
        "payload_message_key",
        "carrier_message_key",
    ):
        value = contract.get(field)
        if isinstance(value, str) and value.strip():
            return _payload_carrier_for_conversation(conversation, value.strip())
    return _payload_carrier_for_conversation(conversation)


def _payload_carrier_for_seed(
    seed_template: Mapping[str, Any],
    conversation: object,
    *,
    explicit: str | None = None,
) -> str:
    if explicit is not None:
        return _payload_carrier_for_conversation(conversation, explicit)
    value = seed_template.get("payload_carrier")
    if isinstance(value, str) and value.strip():
        return _payload_carrier_for_conversation(conversation, value.strip())
    return _payload_carrier_for_conversation(conversation)


def _count_placeholder(value: Any) -> int:
    if isinstance(value, str):
        return value.count(PAYLOAD_PLACEHOLDER)
    if isinstance(value, Mapping):
        return sum(_count_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_placeholder(item) for item in value)
    return 0


__all__ = [
    "ROCKET_CHAT_LEGACY_MESSAGE_KEYS",
    "ROCKET_CHAT_PARTIAL_CORRECTION_KEYS",
    "ROCKET_CHAT_PARTIAL_DEFAULT_PAYLOAD_CARRIER",
    "ROCKET_CHAT_PARTIAL_MESSAGE_KEYS",
    "_conversation_with_placeholder",
    "materialize_adversarial_seed_for_runtime",
    "materialize_seed_template",
    "validate_rocket_chat_seed_template",
]
