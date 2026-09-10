"""WARP task envelopes for the Rocket.Chat conversation workflow family.

The strict static compiler remains the authority for generated conversation
facts and finite grading.  This module owns only the adapter into WARP's
ordinary task, seed, Phase 2, and Phase 4 shape.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping

from warp_taskgen.phase_1.rocket_chat_contracts import (
    ROCKET_CHAT_BENCHMARK,
    ROCKET_CHAT_SEED_METHOD,
    ROCKET_CHAT_SITE,
    RocketChatContractError,
    RocketChatConversation,
    _identity,
    _text,
)
from warp_taskgen.phase_1.rocket_chat_decisions import (
    compile_rocket_chat_task,
    validate_rocket_chat_task,
)
from warp_taskgen.phase_1.rocket_chat_notifications import (
    ROCKET_CHAT_NOTIFICATION_TASK_KIND,
    compile_rocket_chat_notification_task,
    validate_rocket_chat_notification_task,
)

ROCKET_CHAT_CONTRACT_FIELD = "rocket_chat_contract"

_DECISION_STATIC_TASK_KEYS = frozenset(
    {
        "benchmark",
        "site",
        "task_kind",
        "task_id",
        "evaluator_authority",
        "start_urls",
        "conversation",
        "response_schema",
        "expected_decision",
        "reward_function",
        "reader_contract",
    }
)
_NOTIFICATION_STATIC_TASK_KEYS = frozenset(
    {
        "benchmark",
        "site",
        "task_kind",
        "task_id",
        "evaluator_authority",
        "start_urls",
        "conversation",
        "response_schema",
        "expected_decision",
        "notification",
        "action_contract",
        "reward_function",
        "reader_contract",
    }
)
_ENVELOPE_REQUIRED_KEYS = frozenset(
    {
        "id",
        "origin",
        "benchmark",
        "site",
        "sites",
        "instruction",
        "start_urls",
        "data_seed",
        "reward_function",
        ROCKET_CHAT_CONTRACT_FIELD,
    }
)
_CROSS_PHASE_REQUIRED_KEYS = _ENVELOPE_REQUIRED_KEYS - {"origin"}


def project_rocket_chat_static_contract(
    task: Mapping[str, object],
) -> Mapping[str, object]:
    """Return and validate the exact static contract from either task shape."""

    if not isinstance(task, Mapping):
        raise RocketChatContractError("Rocket.Chat task must be a mapping")
    if frozenset(task) in {_DECISION_STATIC_TASK_KEYS, _NOTIFICATION_STATIC_TASK_KEYS}:
        _validate_static_contract(task)
        return task
    nested = task.get(ROCKET_CHAT_CONTRACT_FIELD)
    if not isinstance(nested, Mapping):
        raise RocketChatContractError(
            f"Rocket.Chat WARP task requires {ROCKET_CHAT_CONTRACT_FIELD!r}"
        )
    _validate_static_contract(nested)
    return nested


def _validate_static_contract(task: Mapping[str, object]) -> None:
    if task.get("task_kind") == ROCKET_CHAT_NOTIFICATION_TASK_KIND:
        validate_rocket_chat_notification_task(task)
        return
    validate_rocket_chat_task(task)


def _expected_seed(static: Mapping[str, object]) -> dict[str, object]:
    conversation = static["conversation"]
    messages = conversation.get("messages") if isinstance(conversation, Mapping) else None
    if not isinstance(messages, list):
        raise RocketChatContractError("Rocket.Chat WARP seed requires conversation messages")
    message_by_key = {
        message.get("logical_key"): message for message in messages if isinstance(message, Mapping)
    }
    partial_keys = {"plan", "update", "owner_correction", "date_correction"}
    if set(message_by_key) == partial_keys and len(message_by_key) == len(partial_keys):
        correction_bodies = {
            key: message_by_key[key].get("body") for key in ("owner_correction", "date_correction")
        }
        if not all(isinstance(body, str) and body.strip() for body in correction_bodies.values()):
            raise RocketChatContractError(
                "Rocket.Chat partial-update seed requires both correction signatures"
            )
        # ``render_signature`` remains the generic Phase 2 selector.  The
        # feature-owned ``render_signatures`` map lets the partial pilot prove
        # both correction messages without changing the legacy three-message
        # seed shape.
        seed: dict[str, object] = {
            "mechanism": "editor",
            "payload_carrier": "date_correction",
            "message_logical_keys": [
                "plan",
                "update",
                "owner_correction",
                "date_correction",
            ],
            "render_signature": correction_bodies["owner_correction"],
            "render_signatures": correction_bodies,
            "editor_calls": [
                {
                    "benchmark": ROCKET_CHAT_BENCHMARK,
                    "site": ROCKET_CHAT_SITE,
                    "method": ROCKET_CHAT_SEED_METHOD,
                    "args": {"conversation": copy.deepcopy(conversation)},
                }
            ],
        }
        return seed
    correction_body = next(
        (
            message.get("body")
            for message in messages
            if isinstance(message, Mapping)
            and message.get("logical_key") == "correction"
            and isinstance(message.get("body"), str)
        ),
        None,
    )
    if not isinstance(correction_body, str) or not correction_body.strip():
        raise RocketChatContractError("Rocket.Chat WARP seed requires a correction signature")
    return {
        "mechanism": "editor",
        # The editor consumes nested generated facts, while the generic render
        # gate needs one explicit visible string tied to this single call.
        "render_signature": correction_body,
        "editor_calls": [
            {
                "benchmark": ROCKET_CHAT_BENCHMARK,
                "site": ROCKET_CHAT_SITE,
                "method": ROCKET_CHAT_SEED_METHOD,
                "args": {"conversation": copy.deepcopy(static["conversation"])},
            }
        ],
    }


def _validate_rocket_chat_envelope_fields(
    task: Mapping[str, object],
    *,
    required_keys: frozenset[str],
) -> Mapping[str, object]:
    if not isinstance(task, Mapping):
        raise RocketChatContractError("Rocket.Chat WARP task must be a mapping")
    missing = required_keys - set(task)
    if missing:
        raise RocketChatContractError(
            "Rocket.Chat WARP task is missing fields: "
            + ", ".join(sorted(str(field) for field in missing))
        )
    _identity(task["id"], field="WARP task id")
    _text(task["instruction"], field="WARP task instruction", max_length=1000)
    if task["benchmark"] != ROCKET_CHAT_BENCHMARK:
        raise RocketChatContractError("Rocket.Chat WARP task Benchmark is inconsistent")
    if task["site"] != ROCKET_CHAT_SITE or task["sites"] != [ROCKET_CHAT_SITE]:
        raise RocketChatContractError("Rocket.Chat WARP task Site fields are inconsistent")
    if task.get("task_id") not in (None, ""):
        raise RocketChatContractError("Rocket.Chat WARP task cannot carry a native task_id")

    static = project_rocket_chat_static_contract(task)
    if task["start_urls"] != static["start_urls"]:
        raise RocketChatContractError("Rocket.Chat WARP task start URLs drifted")
    if task["data_seed"] != _expected_seed(static):
        raise RocketChatContractError("Rocket.Chat WARP task seed drifted")
    return static


def validate_rocket_chat_benign_task(task: Mapping[str, object]) -> None:
    """Validate the feature's benign envelope and its static projection."""

    static = _validate_rocket_chat_envelope_fields(
        task,
        required_keys=_ENVELOPE_REQUIRED_KEYS,
    )
    if task["origin"] != "new_task":
        raise RocketChatContractError("Rocket.Chat WARP task origin must be new_task")
    if task["reward_function"] != static["reward_function"]:
        raise RocketChatContractError("Rocket.Chat WARP task reward drifted")


def validate_rocket_chat_cross_phase_task(task: Mapping[str, object]) -> None:
    """Validate benign or Phase 2 composite envelopes without reward confusion."""

    static = _validate_rocket_chat_envelope_fields(
        task,
        required_keys=_CROSS_PHASE_REQUIRED_KEYS,
    )
    reward = task["reward_function"]
    if reward == static["reward_function"]:
        validate_rocket_chat_benign_task(task)
        return
    if not isinstance(reward, Mapping):
        raise RocketChatContractError("Rocket.Chat Phase 2 reward must be a mapping")
    if reward.get("benign_reward") != static["reward_function"]:
        raise RocketChatContractError("Rocket.Chat Phase 2 benign reward drifted")
    adversarial_reward = reward.get("adversarial_reward")
    if not isinstance(adversarial_reward, Mapping) or not adversarial_reward:
        raise RocketChatContractError(
            "Rocket.Chat Phase 2 task requires a non-empty adversarial reward"
        )


def compile_rocket_chat_benign_task(
    conversation: RocketChatConversation,
    *,
    task_id: str,
    instruction: str,
) -> dict[str, object]:
    """Compile one generated conversation into the normal WARP task shape."""

    static = compile_rocket_chat_task(conversation)
    return _compile_rocket_chat_benign_envelope(
        static,
        task_id=task_id,
        instruction=instruction,
    )


def compile_rocket_chat_notification_benign_task(
    conversation: RocketChatConversation,
    *,
    task_id: str,
    instruction: str,
) -> dict[str, object]:
    """Compile one generated notification behavior into a normal WARP task."""

    static = compile_rocket_chat_notification_task(conversation)
    return _compile_rocket_chat_benign_envelope(
        static,
        task_id=task_id,
        instruction=instruction,
    )


def _compile_rocket_chat_benign_envelope(
    static: Mapping[str, object],
    *,
    task_id: str,
    instruction: str,
) -> dict[str, object]:
    stable_id = _identity(task_id, field="WARP task id")
    task_instruction = _text(
        instruction,
        field="WARP task instruction",
        max_length=1000,
    )
    task: dict[str, object] = {
        "id": stable_id,
        "origin": "new_task",
        "benchmark": ROCKET_CHAT_BENCHMARK,
        "site": ROCKET_CHAT_SITE,
        "sites": [ROCKET_CHAT_SITE],
        "instruction": task_instruction,
        "start_urls": copy.deepcopy(static["start_urls"]),
        "data_seed": _expected_seed(static),
        "reward_function": copy.deepcopy(static["reward_function"]),
        ROCKET_CHAT_CONTRACT_FIELD: copy.deepcopy(static),
    }
    validate_rocket_chat_benign_task(task)
    return task


__all__ = [
    "ROCKET_CHAT_CONTRACT_FIELD",
    "compile_rocket_chat_benign_task",
    "compile_rocket_chat_notification_benign_task",
    "project_rocket_chat_static_contract",
    "validate_rocket_chat_benign_task",
    "validate_rocket_chat_cross_phase_task",
]
