"""Feature-local Phase 2 adapters for explicit Rocket.Chat compositions.

Rocket.Chat is intentionally not a default editor/target catalog entry. This
module owns the small target and exposure seam used only when a named TAC
composition is passed by the caller; deterministic plan and typed-seed logic
lives in the sibling feature modules.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from warp_taskgen.phase_1.rocket_chat_contracts import RocketChatContractError
from warp_taskgen.phase_1.rocket_chat_task_envelope import (
    project_rocket_chat_static_contract,
    validate_rocket_chat_benign_task,
)
from warp_taskgen.phase_2.rocket_chat_common import (
    ROCKET_CHAT_ACTION_DESCRIPTION,
    ROCKET_CHAT_ACTION_KIND,
    ROCKET_CHAT_BENCHMARK,
    ROCKET_CHAT_DELIVERY_METHOD,
    ROCKET_CHAT_PLACEHOLDERS,
    ROCKET_CHAT_SITE,
    ROCKET_CHAT_SURFACE,
    ROCKET_CHAT_TARGET_KIND,
    composition_is_partial_update,
    composition_supports_rocket_chat,
)
from warp_taskgen.phase_2.rocket_chat_plans import (
    build_plan,
    build_plans,
    enrich_plans,
    validate_generated_plans,
    validate_plan,
)
from warp_taskgen.phase_2.rocket_chat_plans import (
    validate_materialized_task as _validate_materialized_task,
)
from warp_taskgen.phase_2.rocket_chat_seed import (
    _conversation_with_placeholder,
    _is_partial_message_shape,
    _message_shape,
    _payload_carrier_for_contract,
    materialize_adversarial_seed_for_runtime,
    materialize_seed_template,
    validate_rocket_chat_seed_template,
)
from warp_taskgen.phase_2.runtime_generation import PreparedPhase2Shard
from warp_taskgen.runtime_composition import RuntimeComposition
from warp_taskgen.sites.contracts import ResolvedTarget, TargetingFailure


def unresolved_target_resource(reason: str) -> dict[str, Any]:
    """Return the same fail-closed record shape as the generic resolver."""

    return {
        "kind": None,
        "anchors": {},
        "start_url_resolved": None,
        "attach_surfaces": [],
        "encounter_requirements": {"viewport_budget_chars": 600},
        "layer": None,
        "pending_layer": "L2",
        "reason": reason,
    }


def resolve_target_resource(
    task: Mapping[str, Any],
    runtime_composition: RuntimeComposition | None,
) -> dict[str, Any]:
    """Resolve one benign envelope through the bound Rocket.Chat Site."""

    if not composition_supports_rocket_chat(
        runtime_composition,
        benchmark=task.get("benchmark"),
        site=task.get("site"),
    ):
        return unresolved_target_resource("Rocket.Chat requires an explicit runtime composition")
    try:
        static = project_rocket_chat_static_contract(task)
        validate_rocket_chat_benign_task(task)
        bound = runtime_composition.site_catalog.bind(
            benchmark=ROCKET_CHAT_BENCHMARK,
            site=ROCKET_CHAT_SITE,
            placeholders=ROCKET_CHAT_PLACEHOLDERS,
        )
    except (RocketChatContractError, TypeError, ValueError) as exc:
        return unresolved_target_resource(f"invalid Rocket.Chat envelope: {exc}")
    target = bound.resolve(task, allow_layers=("L1", "L2"))
    if isinstance(target, TargetingFailure):
        return unresolved_target_resource(target.detail)
    if not isinstance(target, ResolvedTarget) or target.kind != "room":
        return unresolved_target_resource("Rocket.Chat Site did not resolve a room")
    conversation = static.get("conversation")
    room_id = conversation.get("room_id") if isinstance(conversation, Mapping) else None
    if not isinstance(room_id, str) or target.anchors.get("room_id") != room_id:
        return unresolved_target_resource(
            "Rocket.Chat target room disagrees with typed conversation"
        )
    start_url = target.start_url_resolved
    if not isinstance(start_url, str) or not start_url.strip():
        return unresolved_target_resource("Rocket.Chat target has no reconstructed room URL")
    message_keys = (
        _message_shape(conversation.get("messages")) if isinstance(conversation, Mapping) else None
    )
    is_partial = _is_partial_message_shape(message_keys)
    if is_partial != composition_is_partial_update(runtime_composition):
        return unresolved_target_resource(
            "Rocket.Chat conversation shape is incompatible with the selected runtime composition"
        )
    return {
        "kind": ROCKET_CHAT_TARGET_KIND,
        "anchors": {"room_id": room_id},
        "start_url_resolved": start_url,
        "benign_read_url": start_url,
        "attach_surfaces": [
            {
                "surface_id": ROCKET_CHAT_SURFACE,
                "attach_method": ROCKET_CHAT_DELIVERY_METHOD,
                "payload_arg": "conversation",
                "write_role": "ordinary_writer",
            }
        ],
        "encounter_requirements": {
            "viewport_budget_chars": 600,
            "requires_exact_thread_panel": True,
            "seed_message_count": len(message_keys) if message_keys is not None else 3,
            **(
                {
                    "required_message_keys": list(message_keys),
                    "payload_carrier": _payload_carrier_for_contract(static, conversation),
                }
                if is_partial and message_keys is not None
                else {}
            ),
        },
        "layer": target.layer,
        "route_variant": "channel_or_group",
    }


def resolve_target_resources(
    tasks: Sequence[Mapping[str, Any]],
    runtime_composition: RuntimeComposition | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Resolve a shard without invoking async L3/L4 or global registries."""

    expanded = [dict(task) for task in tasks if isinstance(task, Mapping)]
    resources = {
        str(task.get("id") or ""): resolve_target_resource(task, runtime_composition)
        for task in expanded
        if str(task.get("id") or "")
    }
    return expanded, resources


def build_exposure_contract(
    *,
    task: Mapping[str, Any],
    benign_target_resource: Mapping[str, Any] | None,
    runtime_composition: RuntimeComposition | None,
) -> dict[str, Any]:
    """Build a single-call exposure contract for one exact RC envelope."""

    task_id = str(task.get("id") or "")
    resource = dict(benign_target_resource or {})
    if not composition_supports_rocket_chat(
        runtime_composition,
        benchmark=task.get("benchmark"),
        site=task.get("site"),
    ):
        return _ineligible_exposure(task_id, "Rocket.Chat requires an explicit runtime composition")
    try:
        static = project_rocket_chat_static_contract(task)
    except (RocketChatContractError, TypeError, ValueError) as exc:
        return _ineligible_exposure(task_id, f"invalid Rocket.Chat envelope: {exc}")
    conversation = static.get("conversation")
    room_id = conversation.get("room_id") if isinstance(conversation, Mapping) else None
    anchors = resource.get("anchors")
    if (
        resource.get("kind") != ROCKET_CHAT_TARGET_KIND
        or not isinstance(anchors, Mapping)
        or anchors.get("room_id") != room_id
    ):
        return _ineligible_exposure(task_id, "Rocket.Chat target room is not exact")
    read_url = resource.get("benign_read_url") or resource.get("start_url_resolved")
    if not isinstance(read_url, str) or not read_url.strip():
        return _ineligible_exposure(task_id, "Rocket.Chat target has no read URL")
    # The benign mapping remains untouched; the call gets a separate copy with
    # exactly one placeholder appended to correction.body.
    conversation_copy = copy.deepcopy(dict(conversation))
    message_keys = _message_shape(conversation_copy.get("messages"))
    if message_keys is None:
        return _ineligible_exposure(
            task_id, "Rocket.Chat conversation message shape is unsupported"
        )
    partial = _is_partial_message_shape(message_keys)
    if partial != composition_is_partial_update(runtime_composition):
        return _ineligible_exposure(
            task_id,
            "Rocket.Chat conversation shape is incompatible with the selected runtime composition",
        )
    try:
        payload_carrier = _payload_carrier_for_contract(static, conversation_copy)
        seeded_conversation = _conversation_with_placeholder(
            copy.deepcopy(conversation_copy),
            payload_carrier=payload_carrier,
        )
    except (TypeError, ValueError) as exc:
        return _ineligible_exposure(task_id, f"Rocket.Chat correction carrier is invalid: {exc}")
    identity_message_keys = list(message_keys)
    return {
        "contract_id": f"rocketchat:{task_id}",
        "benign_task_id": task_id,
        "site": ROCKET_CHAT_SITE,
        "kind": ROCKET_CHAT_TARGET_KIND,
        "mode": "direct_detail",
        "anchors": {"room_id": room_id},
        "benign_read_url": read_url,
        "editor_method": ROCKET_CHAT_DELIVERY_METHOD,
        "target_surface_id": ROCKET_CHAT_SURFACE,
        "payload_arg": "conversation",
        "editor_args_template": {"conversation": seeded_conversation},
        "required_tokens": [],
        "conversation": conversation_copy,
        **(
            {
                "payload_carrier": payload_carrier,
                "message_logical_keys": identity_message_keys,
                "required_message_keys": identity_message_keys,
                "required_identity_tokens": [f"{key}_message_id" for key in identity_message_keys],
                "required_body_digest_tokens": [
                    f"{key}_body_sha256" for key in identity_message_keys
                ],
            }
            if partial
            else {}
        ),
        "verification": {
            "url": read_url,
            "mode": "seed_resource",
            "witness_policy": "painted_exact_message_identity",
            "requires_exact_thread_panel": True,
            **(
                {
                    "message_logical_keys": identity_message_keys,
                    "required_message_keys": identity_message_keys,
                    "payload_carrier": payload_carrier,
                    "requires_all_message_identities": True,
                }
                if partial
                else {}
            ),
        },
        "seed_capability": {
            "schema_version": 1,
            "status": "supported",
            "editor_method": ROCKET_CHAT_DELIVERY_METHOD,
            "target_surface_id": ROCKET_CHAT_SURFACE,
            "payload_arg": "conversation",
            "seed_timing": "pre_task",
            **(
                {
                    "message_logical_keys": identity_message_keys,
                    "required_message_keys": identity_message_keys,
                    "payload_carrier": payload_carrier,
                }
                if partial
                else {}
            ),
        },
        "phase4_exposure": {
            "schema_version": 1,
            "seed_timing": "pre_task",
            "admissible": True,
            "encounter_surface": "benign_read_surface",
            "requires_transition": False,
            "transition_forced_by_task": True,
            "requires_runtime_hook": False,
            "requires_exact_thread_panel": True,
            "requires_painted_readback": True,
            **(
                {
                    "message_logical_keys": identity_message_keys,
                    "required_message_keys": identity_message_keys,
                    "payload_carrier": payload_carrier,
                    "requires_all_message_identities": True,
                }
                if partial
                else {}
            ),
        },
        "surface_route": {
            "surface_id": ROCKET_CHAT_SURFACE,
            "source_field": ROCKET_CHAT_SURFACE,
            "content_capacity": "long_body",
            "visibility": "body",
            "site": ROCKET_CHAT_SITE,
            "kind": ROCKET_CHAT_TARGET_KIND,
            "route": "room.thread_panel",
            **({"payload_carrier": payload_carrier} if partial else {}),
        },
        "adversarial_action_options": [
            {
                "kind": ROCKET_CHAT_ACTION_KIND,
                "description": ROCKET_CHAT_ACTION_DESCRIPTION,
                "impact_tier": 1,
                "action_family": "navigation",
                "capability_family": "external_navigation_review",
            }
        ],
        "eligibility": {"status": "eligible"},
    }


def build_exposure_contracts(
    *,
    tasks: Sequence[Mapping[str, Any]],
    benign_target_resources: Mapping[str, Mapping[str, Any]],
    runtime_composition: RuntimeComposition | None,
) -> dict[str, dict[str, Any]]:
    return {
        task_id: build_exposure_contract(
            task=task,
            benign_target_resource=benign_target_resources.get(task_id),
            runtime_composition=runtime_composition,
        )
        for task in tasks
        if (task_id := str(task.get("id") or ""))
    }


def validate_materialized_task(
    task: Mapping[str, Any],
    *,
    benign_task: Mapping[str, Any],
    runtime_composition: RuntimeComposition | None,
) -> str | None:
    """Validate a final task against freshly reconstructed target/exposure facts."""

    expected_resource = resolve_target_resource(benign_task, runtime_composition)
    if task.get("benign_target_resource") != expected_resource:
        return "Rocket.Chat benign target resource changed from its resolved parent"
    expected_contract = build_exposure_contract(
        task=benign_task,
        benign_target_resource=expected_resource,
        runtime_composition=runtime_composition,
    )
    if task.get("exposure_contract") != expected_contract:
        return "Rocket.Chat exposure contract changed from its resolved parent"
    return _validate_materialized_task(
        task,
        benign_task=benign_task,
        exposure_contract=expected_contract,
        runtime_composition=runtime_composition,
    )


def eligible_tasks(
    tasks: Sequence[dict[str, Any]],
    resources: Mapping[str, Mapping[str, Any]],
    contracts: Mapping[str, Mapping[str, Any]],
    runtime_composition: RuntimeComposition | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply exact composition, target, and exposure gates."""

    eligible: list[dict[str, Any]] = []
    drops: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("id") or "")
        resource = resources.get(task_id)
        contract = contracts.get(task_id)
        reason: str | None = None
        if not composition_supports_rocket_chat(
            runtime_composition,
            benchmark=task.get("benchmark"),
            site=task.get("site"),
        ):
            reason = "Rocket.Chat requires an explicit runtime composition"
        elif not isinstance(resource, Mapping) or resource.get("kind") != ROCKET_CHAT_TARGET_KIND:
            reason = str((resource or {}).get("reason") or "unresolved_target_resource")
        elif (
            not isinstance(contract, Mapping)
            or contract.get("eligibility", {}).get("status") != "eligible"
        ):
            eligibility = contract.get("eligibility") if isinstance(contract, Mapping) else None
            reason = (
                str(eligibility.get("reason"))
                if isinstance(eligibility, Mapping)
                else "exposure_contract_ineligible"
            )
        if reason is not None:
            drops.append(
                {
                    "task_id": task_id,
                    "origin": str(task.get("origin") or ""),
                    "kind": resource.get("kind") if isinstance(resource, Mapping) else None,
                    "reason": reason,
                    "anchors": dict(resource.get("anchors") or {})
                    if isinstance(resource, Mapping)
                    else {},
                    "available_tokens": [],
                    "contract_id": contract.get("contract_id")
                    if isinstance(contract, Mapping)
                    else None,
                    "target_surface_id": contract.get("target_surface_id")
                    if isinstance(contract, Mapping)
                    else None,
                }
            )
            continue
        eligible.append(task)
    return eligible, drops


class RocketChatPhase2Generation:
    """Deep feature implementation consumed by an explicit runtime composition."""

    def applies_to(self, *, benchmark: object, site: object) -> bool:
        return (
            str(benchmark or "").strip().lower() == ROCKET_CHAT_BENCHMARK
            and str(site or "").strip().lower() == ROCKET_CHAT_SITE
        )

    def prepare_shard(
        self,
        tasks: Sequence[Mapping[str, Any]],
        runtime_composition: RuntimeComposition,
    ) -> PreparedPhase2Shard:
        expanded, resources = resolve_target_resources(tasks, runtime_composition)
        contracts = build_exposure_contracts(
            tasks=expanded,
            benign_target_resources=resources,
            runtime_composition=runtime_composition,
        )
        eligible, drops = eligible_tasks(
            expanded,
            resources,
            contracts,
            runtime_composition,
        )
        plans = build_plans(eligible, contracts, runtime_composition=runtime_composition)
        for plan in plans:
            benign_id = str(plan.get("benign_task_id") or "")
            contract = contracts.get(benign_id)
            if not isinstance(contract, Mapping):
                raise ValueError(
                    f"Rocket.Chat plan {plan.get('id', '?')!r} has no exposure contract"
                )
            plan["target_surface_id"] = str(contract.get("target_surface_id") or "")
            plan["seed_template"] = materialize_seed_template(
                contract,
                runtime_composition=runtime_composition,
            )
            plan["delivery_mechanism"] = "editor"
        return PreparedPhase2Shard(
            tasks=eligible,
            benign_target_resources=resources,
            exposure_contracts=contracts,
            eligibility_drops=drops,
            plans=plans,
        )

    def validate_and_enrich_plans(
        self,
        plans: Sequence[dict[str, Any]],
        benign_tasks: Sequence[dict[str, Any]],
        *,
        exposure_contracts: Mapping[str, Mapping[str, Any]],
        runtime_composition: RuntimeComposition,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        validated, errors = validate_generated_plans(
            plans,
            benign_tasks,
            exposure_contracts=exposure_contracts,
            runtime_composition=runtime_composition,
        )
        return enrich_plans(validated), errors

    def validate_plan(
        self,
        plan: object,
        *,
        index: int,
        benign_by_id: Mapping[str, Mapping[str, Any]],
        exposure_contracts: Mapping[str, Mapping[str, Any]],
        runtime_composition: RuntimeComposition,
    ) -> str | None:
        return validate_plan(
            plan,
            index=index,
            benign_by_id=benign_by_id,
            exposure_contracts=exposure_contracts,
            runtime_composition=runtime_composition,
        )

    def validate_materialized_task(
        self,
        task: Mapping[str, Any],
        *,
        benign_task: Mapping[str, Any],
        runtime_composition: RuntimeComposition,
    ) -> str | None:
        return validate_materialized_task(
            task,
            benign_task=benign_task,
            runtime_composition=runtime_composition,
        )


ROCKET_CHAT_PHASE2_GENERATION = RocketChatPhase2Generation()


def _ineligible_exposure(task_id: str, reason: str) -> dict[str, Any]:
    return {
        "contract_id": f"rocketchat:{task_id}:ineligible",
        "benign_task_id": task_id,
        "site": ROCKET_CHAT_SITE,
        "kind": None,
        "anchors": {},
        "mode": "ineligible",
        "phase4_exposure": {
            "schema_version": 1,
            "seed_timing": "pre_task",
            "admissible": False,
            "reason": reason,
            "encounter_surface": "none",
            "requires_transition": False,
            "transition_forced_by_task": False,
            "requires_runtime_hook": False,
        },
        "seed_capability": {"status": "unsupported", "reason": reason},
        "eligibility": {"status": "ineligible", "reason": reason},
    }


__all__ = [
    "ROCKET_CHAT_PHASE2_GENERATION",
    "RocketChatPhase2Generation",
    "build_exposure_contract",
    "build_exposure_contracts",
    "build_plan",
    "build_plans",
    "composition_supports_rocket_chat",
    "eligible_tasks",
    "enrich_plans",
    "materialize_adversarial_seed_for_runtime",
    "materialize_seed_template",
    "resolve_target_resource",
    "resolve_target_resources",
    "unresolved_target_resource",
    "validate_generated_plans",
    "validate_materialized_task",
    "validate_plan",
    "validate_rocket_chat_seed_template",
]
