"""Concrete dispatch for generated workflow feature owners.

Generic Phase 1 orchestration calls this seam without knowing which Site or
family owns a generated row. Feature compilers retain semantic-slot, host
reconstruction, and post-validation restoration behavior.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from warp_taskgen.phase_1.gitlab_compare_compiled_validation import (
    is_host_compiled_comparison_task,
)
from warp_taskgen.phase_1.gitlab_compare_decide_generation import (
    compile_phase1_gitlab_compare_act_task,
    compile_phase1_gitlab_compare_decide_task,
    gitlab_compare_act_generation_contract,
    gitlab_compare_decide_generation_contract,
    gitlab_compare_generation_prompt_addendum,
    gitlab_compare_semantic_key,
)
from warp_taskgen.phase_1.rocket_chat_contracts import ROCKET_CHAT_EVALUATOR_NAME
from warp_taskgen.phase_1.rocket_chat_generation import (
    compile_phase1_rocket_chat_decision_task,
    compile_phase1_rocket_chat_notification_task,
    restore_phase1_rocket_chat_decision_task,
    restore_phase1_rocket_chat_notification_task,
    rocket_chat_decision_generation_contract,
    rocket_chat_notification_generation_contract,
)
from warp_taskgen.phase_1.rocket_chat_generation_prompt import (
    rocket_chat_generation_prompt_addendum,
)
from warp_taskgen.phase_1.rocket_chat_notifications import (
    ROCKET_CHAT_NOTIFICATION_EVALUATOR_NAME,
)
from warp_taskgen.phase_1.rocket_chat_partial_update import (
    compile_phase1_rocket_chat_partial_update_task,
    restore_phase1_rocket_chat_partial_update_task,
    rocket_chat_partial_update_generation_contract,
)
from warp_taskgen.phases.phase_1_task_cards import (
    task_card_generation_prompt_addendum,
    task_card_plan_for_site,
)


def compile_model_owned_content(
    tasks: Any,
    *,
    task_card_plan: Mapping[str, Any] | None,
) -> Any:
    """Compile model-owned semantic slots before ordinary validation."""

    if not isinstance(tasks, list) or not isinstance(task_card_plan, Mapping):
        return tasks
    cards = _cards_by_id(task_card_plan)
    compiled: list[Any] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            compiled.append(task)
            continue
        card = cards.get(str(task.get("task_card_id") or ""))
        if rocket_chat_partial_update_generation_contract(card) is not None:
            compiled.append(compile_phase1_rocket_chat_partial_update_task(task, task_card=card))
        elif rocket_chat_notification_generation_contract(card) is not None:
            compiled.append(compile_phase1_rocket_chat_notification_task(task, task_card=card))
        elif rocket_chat_decision_generation_contract(card) is not None:
            compiled.append(compile_phase1_rocket_chat_decision_task(task, task_card=card))
        elif gitlab_compare_act_generation_contract(card) is not None:
            compiled.append(compile_phase1_gitlab_compare_act_task(task, task_card=card))
        elif gitlab_compare_decide_generation_contract(card) is not None:
            compiled.append(compile_phase1_gitlab_compare_decide_task(task, task_card=card))
        else:
            compiled.append(task)
    return compiled


def restore_compiled_tasks(
    tasks: Sequence[Any],
    *,
    task_card_plan: Mapping[str, Any] | None,
) -> list[Any]:
    """Restore or compile feature rows after ordinary validation."""

    return [
        restore_compiled_task(task, task_card_plan=task_card_plan)
        if isinstance(task, Mapping)
        else task
        for task in tasks
    ]


def restore_compiled_task(
    task: Mapping[str, Any],
    *,
    task_card_plan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Dispatch one validated row to its concrete feature owner."""

    item = dict(task)
    if not isinstance(task_card_plan, Mapping):
        return item
    card = _cards_by_id(task_card_plan).get(str(task.get("task_card_id") or ""))
    if not isinstance(card, Mapping):
        return item
    if rocket_chat_partial_update_generation_contract(card) is not None:
        return restore_phase1_rocket_chat_partial_update_task(task, task_card=card)
    if rocket_chat_notification_generation_contract(card) is not None:
        return restore_phase1_rocket_chat_notification_task(task, task_card=card)
    if rocket_chat_decision_generation_contract(card) is not None:
        return restore_phase1_rocket_chat_decision_task(task, task_card=card)
    if gitlab_compare_act_generation_contract(card) is not None:
        return compile_phase1_gitlab_compare_act_task(task, task_card=card)
    if gitlab_compare_decide_generation_contract(card) is not None:
        return compile_phase1_gitlab_compare_decide_task(task, task_card=card)
    return item


def generation_prompt_addendum(
    task_card_plan: Mapping[str, Any] | None,
    *,
    site_name: str | None = None,
    _task_number_start: int | None = None,
) -> str:
    """Return feature and optional exact-allocation prompt extensions for one site plan.

    ``_task_number_start`` is private sliced-generation context.  It is passed
    only to the allocation addendum so resume fingerprints and parent-plan
    metadata remain unchanged.
    """

    gitlab = gitlab_compare_generation_prompt_addendum(task_card_plan)
    feature_addendum = gitlab or rocket_chat_generation_prompt_addendum(task_card_plan)
    allocation_addendum = task_card_generation_prompt_addendum(
        task_card_plan,
        site_name=site_name,
        _task_number_start=_task_number_start,
    )
    if feature_addendum and allocation_addendum:
        return f"{feature_addendum}\n\n{allocation_addendum}"
    return feature_addendum or allocation_addendum


def generation_prompt_fingerprint_inputs(
    task_card_plan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Expose feature prompt inputs through the existing resume fingerprint."""

    allocation_prompt_addenda: dict[str, str] = {}
    if isinstance(task_card_plan, Mapping):
        sites = sorted(
            {
                str(card.get("site") or "").strip()
                for card in task_card_plan.get("task_cards", [])
                if isinstance(card, Mapping)
                and str(card.get("status", "active")) == "active"
                and str(card.get("site") or "").strip()
            }
        )
        allocation_prompt_addenda = {
            site_name: task_card_generation_prompt_addendum(
                task_card_plan_for_site(task_card_plan, site_name),
                site_name=site_name,
            )
            for site_name in sites
        }

    return {
        "gitlab_comparison_prompt_addendum": gitlab_compare_generation_prompt_addendum(
            task_card_plan_for_site(task_card_plan, "gitlab")
        ),
        "rocket_chat_prompt_addendum": rocket_chat_generation_prompt_addendum(
            task_card_plan_for_site(task_card_plan, "rocketchat")
        ),
        "task_card_generation_prompt_addenda": allocation_prompt_addenda,
    }


def host_compiled_evaluator_types(
    task_card_plan: Mapping[str, Any] | None,
) -> frozenset[str]:
    """Return exact evaluators enabled by authored feature contracts.

    These evaluator names are not available to ordinary model-authored tasks.
    The opt-in exists only because the corresponding feature compiler replaces
    the model's semantic slots with a host-owned reward contract.
    """

    if not isinstance(task_card_plan, Mapping):
        return frozenset()
    evaluator_types: set[str] = set()
    for card in _cards_by_id(task_card_plan).values():
        if rocket_chat_partial_update_generation_contract(card) is not None:
            evaluator_types.add(ROCKET_CHAT_EVALUATOR_NAME)
        elif rocket_chat_notification_generation_contract(card) is not None:
            evaluator_types.add(ROCKET_CHAT_NOTIFICATION_EVALUATOR_NAME)
        elif rocket_chat_decision_generation_contract(card) is not None:
            evaluator_types.add(ROCKET_CHAT_EVALUATOR_NAME)
    return frozenset(evaluator_types)


def validated_host_action_contract(
    task: Mapping[str, Any],
    *,
    task_card: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return feature-owned action provenance after canonical revalidation."""

    if gitlab_compare_act_generation_contract(task_card) is None:
        return None
    card_id = str(task_card.get("id") or "")
    if not card_id or not isinstance(task.get("comparison_act_contract"), Mapping):
        return None
    try:
        if not is_host_compiled_comparison_task(
            task,
            act=True,
            task_card_id=card_id,
        ):
            return None
    except (TypeError, ValueError):
        return None
    provenance = task.get("task_provenance")
    contract = provenance.get("benign_action_contract") if isinstance(provenance, Mapping) else None
    return dict(contract) if isinstance(contract, Mapping) else None


def owns_host_action_contract(task_card: Mapping[str, Any] | None) -> bool:
    """Return whether a generated-workflow feature owns this card's action reward."""

    return gitlab_compare_act_generation_contract(task_card) is not None


def owns_model_generated_content(task_card: Mapping[str, Any] | None) -> bool:
    """Return whether a feature owns semantic content generation for a card."""

    return any(
        contract(task_card) is not None
        for contract in (
            rocket_chat_partial_update_generation_contract,
            rocket_chat_notification_generation_contract,
            rocket_chat_decision_generation_contract,
            gitlab_compare_act_generation_contract,
            gitlab_compare_decide_generation_contract,
        )
    )


def stable_answer_diversity_key(
    task: Mapping[str, Any],
    *,
    task_card_index: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    """Return a feature-owned semantic key beyond generic answer shape.

    Generated GitLab comparison rows carry a host-owned exact response contract;
    their diversity is represented by the feature's canonical worlds instead of
    the generic binary answer-shape families.  Only an active card and a
    canonical host-compiled row may use this seam.  Invalid or copied rows
    return ``None`` and remain on the ordinary validation path.
    """

    if not isinstance(task_card_index, Mapping):
        return None
    card_id = task.get("task_card_id")
    if not isinstance(card_id, str):
        return None
    card = task_card_index.get(card_id)
    if not isinstance(card, Mapping):
        return None
    contract = gitlab_compare_decide_generation_contract(card)
    if contract is None:
        contract = gitlab_compare_act_generation_contract(card)
    if contract is None:
        return None
    return gitlab_compare_semantic_key(
        task,
        task_card_id=card_id,
        act=contract.get("family") == "gitlab_compare_act",
    )


def _cards_by_id(task_card_plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(card.get("id")): card
        for card in task_card_plan.get("task_cards", [])
        if isinstance(card, Mapping) and isinstance(card.get("id"), str)
    }


__all__ = [
    "compile_model_owned_content",
    "generation_prompt_addendum",
    "generation_prompt_fingerprint_inputs",
    "host_compiled_evaluator_types",
    "owns_host_action_contract",
    "owns_model_generated_content",
    "restore_compiled_task",
    "restore_compiled_tasks",
    "stable_answer_diversity_key",
    "validated_host_action_contract",
]
