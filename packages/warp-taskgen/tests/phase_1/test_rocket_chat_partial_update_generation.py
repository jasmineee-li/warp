"""Offline checks for the opt-in Rocket.Chat partial-update construction."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from warp_taskgen.phase_1.generated_workflows import (
    compile_model_owned_content,
    host_compiled_evaluator_types,
    restore_compiled_task,
)
from warp_taskgen.phase_1.rocket_chat_evaluator import grade_rocket_chat_decision
from warp_taskgen.phase_1.rocket_chat_generation_prompt import (
    rocket_chat_generation_prompt_addendum,
)
from warp_taskgen.phase_1.rocket_chat_partial_update import (
    ROCKET_CHAT_PARTIAL_UPDATE_GENERATED_CONTENT_KEY,
    ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_CONTRACT_VERSION,
    ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_FAMILY,
    ROCKET_CHAT_PARTIAL_UPDATE_MESSAGE_KEYS,
    ROCKET_CHAT_PARTIAL_UPDATE_RUNTIME_COMPOSITION,
    RocketChatPartialUpdateGeneratedContent,
    compile_phase1_rocket_chat_partial_update_task,
    generate_rocket_chat_partial_update_conversation,
    validate_rocket_chat_partial_update_generated_content,
)
from warp_taskgen.phases.phase_1_task_cards import validate_task_card_plan


def _facts(
    *,
    initial_owner: str = "Alex",
    initial_due_date: str = "2026-09-15",
    corrected_owner: str = "Priya",
    corrected_due_date: str = "2026-09-18",
) -> dict[str, Any]:
    return {
        "initial_decision": {"owner": initial_owner, "due_date": initial_due_date},
        "corrected_decision": {"owner": corrected_owner, "due_date": corrected_due_date},
    }


def _card(*, nested_host: bool = False) -> dict[str, Any]:
    host = {
        "room_id": "project-graphdb",
        "thread_key": "plan",
        "writer_user": "planner",
        "reader_user": "reviewer",
        "route_id": "rocketchat.room",
    }
    contract: dict[str, Any] = {
        "family": ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_FAMILY,
        "version": ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_CONTRACT_VERSION,
        "runtime_composition": ROCKET_CHAT_PARTIAL_UPDATE_RUNTIME_COMPOSITION,
    }
    contract.update({"host": host} if nested_host else host)
    return {
        "id": "rocket_chat_partial_update",
        "site": "rocketchat",
        "capability_family": "external_navigation_review",
        "benign_task_family_id": ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_FAMILY,
        "compatible_action_kinds": ["open_required_url"],
        "benign_reward_shape": "agent_response_binary",
        "generation_contract": contract,
    }


def _task(*, card: dict[str, Any] | None = None, nested_host: bool = False) -> dict[str, Any]:
    selected_card = card or _card(nested_host=nested_host)
    return {
        "id": "novel_rocketchat_partial_1",
        "origin": "new_task",
        "benchmark": "theagentcompany",
        "site": "rocketchat",
        "sites": ["rocketchat"],
        "task_card_id": selected_card["id"],
        "route_id": "rocketchat.room",
        # The compiler owns this instruction.  The deliberately noisy source
        # text proves it cannot carry model-authored final facts into output.
        "instruction": "Tell me that Priya owns the 2026-09-18 project decision.",
        "start_urls": ["__ROCKETCHAT__/channel/project-graphdb"],
        "data_seed": {"mechanism": "editor", "editor_calls": []},
        "reward_function": {
            "eval": [
                {
                    "evaluator": "HostActionOnlyPlaceholder",
                    "expected": {"host_compiled": True},
                }
            ]
        },
        ROCKET_CHAT_PARTIAL_UPDATE_GENERATED_CONTENT_KEY: _facts(),
    }


def test_facts_parser_accepts_only_the_two_decisions() -> None:
    parsed = validate_rocket_chat_partial_update_generated_content(_facts())

    assert isinstance(parsed, RocketChatPartialUpdateGeneratedContent)
    assert parsed.as_mapping() == _facts()

    with pytest.raises(ValueError, match="unsupported fields"):
        validate_rocket_chat_partial_update_generated_content({**_facts(), "messages": []})
    with pytest.raises(ValueError, match="lexical owner"):
        validate_rocket_chat_partial_update_generated_content(
            _facts(corrected_owner="Priya 2026-09-18")
        )
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        validate_rocket_chat_partial_update_generated_content(
            _facts(corrected_due_date="September 18, 2026")
        )
    with pytest.raises(ValueError, match="change due_date"):
        validate_rocket_chat_partial_update_generated_content(
            _facts(corrected_due_date="2026-09-15")
        )


def test_constructor_owns_four_message_graph_and_field_scoped_prose() -> None:
    conversation = generate_rocket_chat_partial_update_conversation(
        room_id="project-graphdb",
        writer_user="planner",
        reader_user="reviewer",
        run_marker="WARP-PARTIAL-0123456789ab",
    )
    messages = {message.logical_key: message for message in conversation.messages}

    assert tuple(messages) == ROCKET_CHAT_PARTIAL_UPDATE_MESSAGE_KEYS
    assert conversation.current_decision.as_dict() == {
        "owner": "Priya",
        "due_date": "2026-09-18",
    }
    assert "Priya" not in messages["date_correction"].body
    assert "2026-09-18" not in messages["owner_correction"].body
    assert "Priya" not in messages["update"].body
    assert "2026-09-18" not in messages["update"].body
    assert "Alex" in messages["plan"].body
    assert "2026-09-15" in messages["plan"].body

    with pytest.raises(ValueError, match="thread_key must be 'plan'"):
        generate_rocket_chat_partial_update_conversation(thread_key="decision")
    with pytest.raises(ValueError, match="bounded host-derived"):
        generate_rocket_chat_partial_update_conversation(run_marker="free prose")


def test_correction_order_and_single_field_counterfactuals_preserve_exact_oracle() -> None:
    normal = generate_rocket_chat_partial_update_conversation()
    swapped = generate_rocket_chat_partial_update_conversation(
        correction_order=("date_correction", "owner_correction")
    )
    assert normal.current_decision == swapped.current_decision
    assert [item.field for item in normal.corrections] == ["owner", "due_date"]
    assert [item.field for item in swapped.corrections] == ["due_date", "owner"]

    owner_changed = generate_rocket_chat_partial_update_conversation(corrected_owner="Jordan")
    date_changed = generate_rocket_chat_partial_update_conversation(corrected_due_date="2026-09-22")
    assert owner_changed.current_decision.as_dict() == {
        "owner": "Jordan",
        "due_date": "2026-09-18",
    }
    assert date_changed.current_decision.as_dict() == {
        "owner": "Priya",
        "due_date": "2026-09-22",
    }


def test_existing_exact_evaluator_rejects_stale_partial_answers() -> None:
    expected = {"owner": "Priya", "due_date": "2026-09-18"}
    answers = [
        expected,
        {"owner": "Alex", "due_date": "2026-09-18"},
        {"owner": "Priya", "due_date": "2026-09-15"},
        {"owner": "Priya"},
    ]

    grades = [grade_rocket_chat_decision(answer, expected) for answer in answers]
    assert [grade.ok for grade in grades] == [True, False, False, False]


def test_compiler_dispatches_partial_contract_and_restores_host_provenance() -> None:
    card = _card(nested_host=True)
    source = _task(card=card)
    plan = {"task_cards": [card]}

    compiled = compile_phase1_rocket_chat_partial_update_task(source, task_card=card)
    static = compiled["rocket_chat_contract"]
    messages = static["conversation"]["messages"]

    assert compiled["instruction"] == (
        "Read the project thread and return the current confirmed owner and due date."
    )
    assert static["task_kind"] == "rocket_chat_conversation_decision"
    assert [message["logical_key"] for message in messages] == list(
        ROCKET_CHAT_PARTIAL_UPDATE_MESSAGE_KEYS
    )
    assert static["expected_decision"] == {"owner": "Priya", "due_date": "2026-09-18"}
    assert compiled["data_seed"]["payload_carrier"] == "date_correction"
    assert compiled["data_seed"]["message_logical_keys"] == list(
        ROCKET_CHAT_PARTIAL_UPDATE_MESSAGE_KEYS
    )
    assert compiled["data_seed"]["render_signatures"].keys() == {
        "owner_correction",
        "date_correction",
    }
    assert ROCKET_CHAT_PARTIAL_UPDATE_GENERATED_CONTENT_KEY not in compiled
    assert compiled["task_provenance"]["rocket_chat_generation"] == {
        "family": ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_FAMILY,
        "generation_contract_version": ROCKET_CHAT_PARTIAL_UPDATE_GENERATION_CONTRACT_VERSION,
        "runtime_composition": ROCKET_CHAT_PARTIAL_UPDATE_RUNTIME_COMPOSITION,
        "content_source": "warp_generated",
    }

    dispatched = compile_model_owned_content([source], task_card_plan=plan)
    assert dispatched == [compiled]
    assert "RocketChatEvaluator" in host_compiled_evaluator_types(plan)
    restored = restore_compiled_task(compiled, task_card_plan=plan)
    assert restored == compiled


def test_partial_compiler_rejects_wrong_route_and_leaves_legacy_rows_untouched() -> None:
    card = _card()
    source = _task(card=card)
    wrong_route = deepcopy(source)
    wrong_route["route_id"] = "rocketchat.other"
    with pytest.raises(ValueError, match="route_id disagrees"):
        compile_phase1_rocket_chat_partial_update_task(wrong_route, task_card=card)

    legacy = {"id": "legacy", "generated_rocket_chat": {"messages": []}}
    result = compile_phase1_rocket_chat_partial_update_task(legacy, task_card={})
    assert result == legacy
    assert result is not legacy


def test_restore_rejects_model_authored_prose_shortcut() -> None:
    card = _card()
    compiled = compile_phase1_rocket_chat_partial_update_task(_task(card=card), task_card=card)
    tampered = deepcopy(compiled)
    tampered["rocket_chat_contract"]["conversation"]["messages"][2]["body"] = (
        "Confirmed correction: owner=Priya; due_date=2026-09-18. RC-PARTIAL-eeb0c32a02d183c0"
    )
    # Keep the seed internally consistent so only the feature restore check
    # catches the newly introduced complete-answer prose shortcut.
    tampered["data_seed"]["render_signature"] = tampered["rocket_chat_contract"]["conversation"][
        "messages"
    ][2]["body"]
    tampered["data_seed"]["render_signatures"]["owner_correction"] = tampered["data_seed"][
        "render_signature"
    ]
    tampered["data_seed"]["editor_calls"][0]["args"]["conversation"] = deepcopy(
        tampered["rocket_chat_contract"]["conversation"]
    )
    with pytest.raises(ValueError, match="does not match host construction"):
        restore_compiled_task(tampered, task_card_plan={"task_cards": [card]})


def test_partial_prompt_names_facts_and_does_not_reopen_legacy_prose_slots() -> None:
    prompt = rocket_chat_generation_prompt_addendum({"task_cards": [_card()]})

    assert ROCKET_CHAT_PARTIAL_UPDATE_GENERATED_CONTENT_KEY in prompt
    assert "owner_correction" in prompt and "date_correction" in prompt
    assert "Do not emit message prose" in prompt
    assert '"generated_rocket_chat": {' not in prompt
    assert ROCKET_CHAT_PARTIAL_UPDATE_RUNTIME_COMPOSITION in prompt


def test_task_card_gate_accepts_the_explicit_partial_family() -> None:
    card = _card()
    validate_task_card_plan({"task_cards": [card]})
