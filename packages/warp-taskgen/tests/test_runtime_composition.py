from __future__ import annotations

import pytest

from warp_taskgen.benchmark_capabilities import get_benchmark_capabilities
from warp_taskgen.runtime_composition import (
    CLASSIFIEDS_LISTING_REPLY_POC,
    DEFAULT_RUNTIME_COMPOSITION,
    ROCKET_CHAT_CONVERSATION_DECISION_POC,
    ROCKET_CHAT_CONVERSATION_NOTIFICATION_POC,
    RUNTIME_COMPOSITION_CHOICES,
    RuntimeComposition,
    classifieds_listing_reply_poc,
    rocket_chat_conversation_decision_poc,
    rocket_chat_conversation_notification_poc,
    runtime_composition_for_name,
)


def test_classifieds_runtime_composition_is_explicit_and_isolated() -> None:
    composition = classifieds_listing_reply_poc()

    assert composition.name == CLASSIFIEDS_LISTING_REPLY_POC
    assert composition.site_catalog.sites == ("classifieds",)
    assert composition.seed_registry.get("visualwebarena", "classifieds") is not None
    assert composition.feasibility_policy_catalog.get("visualwebarena", "classifieds") is not None
    assert composition.strict_seed_cleanup is True
    assert composition.seed_token_scope == "method"
    assert composition.strict_site_planning is True


def test_rocket_chat_runtime_composition_is_explicit_and_non_default() -> None:
    composition = rocket_chat_conversation_decision_poc()

    assert composition.name == ROCKET_CHAT_CONVERSATION_DECISION_POC
    assert composition.site_catalog.sites == ("rocketchat",)
    assert composition.seed_registry.get("theagentcompany", "rocketchat") is not None
    assert composition.feasibility_policy_catalog.get("theagentcompany", "rocketchat") is not None
    assert composition.strict_seed_cleanup is True
    assert (
        runtime_composition_for_name(ROCKET_CHAT_CONVERSATION_DECISION_POC).name == composition.name
    )
    capabilities = get_benchmark_capabilities("theagentcompany")
    assert capabilities.supports("phase_2_generation") is False
    assert capabilities.supports("phase_2_feasibility") is False
    assert capabilities.supports("phase_4_execution") is False


def test_rocket_chat_notification_composition_is_explicit_and_feature_owned() -> None:
    decision = rocket_chat_conversation_decision_poc()
    notification = rocket_chat_conversation_notification_poc()

    assert notification.name == ROCKET_CHAT_CONVERSATION_NOTIFICATION_POC
    assert notification.site_catalog.sites == ("rocketchat",)
    assert notification.seed_registry.get("theagentcompany", "rocketchat") is not None
    assert notification.strict_seed_cleanup is True
    assert notification.reward_evidence_loader is not None
    assert decision.reward_evidence_loader is None
    assert (
        runtime_composition_for_name(ROCKET_CHAT_CONVERSATION_NOTIFICATION_POC).name
        == notification.name
    )


def test_unnamed_run_resolves_the_default_composition_and_unknown_fails_closed() -> None:
    from warp_taskgen.seeding.site_contracts import default_seed_registry
    from warp_taskgen.sites.catalog import default_catalog

    for name in (None, "", "  ", DEFAULT_RUNTIME_COMPOSITION, "  DEFAULT  "):
        composition = runtime_composition_for_name(name)
        assert composition.name == DEFAULT_RUNTIME_COMPOSITION
        assert composition.site_catalog is default_catalog()
        assert set(composition.seed_registry.registrations) == set(
            default_seed_registry().registrations
        )
        assert composition.seed_token_scope == "kind"
        assert composition.strict_site_planning is False

    # Nothing is memoized: each resolution assembles its own bundle.
    assert runtime_composition_for_name(None) is not runtime_composition_for_name(None)
    assert DEFAULT_RUNTIME_COMPOSITION in RUNTIME_COMPOSITION_CHOICES

    with pytest.raises(ValueError, match="unknown runtime composition"):
        runtime_composition_for_name("classifieds")


def test_default_runtime_composition_binds_todays_default_catalogs() -> None:
    from warp_taskgen.phase_2.phase_2c.policy import default_feasibility_policy_catalog
    from warp_taskgen.seeding.site_contracts import default_seed_registry
    from warp_taskgen.sites.catalog import default_catalog

    composition = RuntimeComposition.default()

    assert composition.name == DEFAULT_RUNTIME_COMPOSITION == "default"
    assert set(composition.seed_registry.registrations) == {
        ("webarena_verified", "gitlab"),
        ("webarena_verified", "reddit"),
    }
    assert set(composition.seed_registry.registrations) == set(
        default_seed_registry().registrations
    )
    assert composition.site_catalog is default_catalog()
    assert set(composition.feasibility_policy_catalog.policies) == set(
        default_feasibility_policy_catalog().policies
    )
    assert composition.benchmark_capabilities is None
    assert composition.reader_preflight is None
    assert composition.phase_2_admission is None
    assert composition.reward_evidence_loader is None
    assert composition.phase_2_generation is None
    assert composition.strict_seed_cleanup is False
    assert composition.seed_token_scope == "kind"
    assert composition.strict_site_planning is False


def test_default_runtime_composition_registers_nothing_process_wide() -> None:
    first = RuntimeComposition.default()
    second = RuntimeComposition.default()

    assert first is not second
    assert first.seed_registry is not second.seed_registry
    assert set(first.seed_registry.registrations) == set(second.seed_registry.registrations)


def test_empty_seed_registry_composition_fails_closed_with_no_editor() -> None:
    from dataclasses import replace

    from warp_taskgen import seeding
    from warp_taskgen.seeding.site_contracts import SeedSiteRegistry

    composition = replace(
        RuntimeComposition.default(),
        seed_registry=SeedSiteRegistry.from_registrations(()),
    )

    with pytest.raises(seeding.EditorError) as raised:
        seeding.apply_data_seed(
            {
                "mechanism": "editor",
                "editor_calls": [
                    {
                        "benchmark": "webarena_verified",
                        "site": "reddit",
                        "method": "create_submission",
                        "args": {"forum_name": "books", "title_template": "Thread"},
                    }
                ],
            },
            {"site_name": "reddit", "site_url": "http://reddit.test"},
            seed_registry=composition.seed_registry,
        )

    assert raised.value.kind == "unsupported_site"
    assert "no editor registered" in raised.value.detail


def test_runtime_composition_is_frozen() -> None:
    composition = classifieds_listing_reply_poc()

    with pytest.raises((AttributeError, TypeError)):
        composition.name = "other"  # type: ignore[misc]


def test_runtime_composition_rejects_wrong_catalog_types() -> None:
    composition = classifieds_listing_reply_poc()

    with pytest.raises(TypeError, match="site_catalog"):
        RuntimeComposition(
            name=composition.name,
            site_catalog=object(),  # type: ignore[arg-type]
            seed_registry=composition.seed_registry,
            feasibility_policy_catalog=composition.feasibility_policy_catalog,
            seed_token_scope=composition.seed_token_scope,
            strict_site_planning=composition.strict_site_planning,
        )


def test_runtime_composition_rejects_non_callable_reward_evidence_loader() -> None:
    from dataclasses import replace

    composition = classifieds_listing_reply_poc()

    with pytest.raises(TypeError, match="reward_evidence_loader"):
        replace(composition, reward_evidence_loader="not-callable")


def test_partial_update_composition_is_explicit_and_keeps_default_closed() -> None:
    from warp_taskgen.runtime_composition import (
        ROCKET_CHAT_PARTIAL_UPDATE_DECISION_POC,
        rocket_chat_partial_update_decision_poc,
    )

    composition = rocket_chat_partial_update_decision_poc()
    assert composition.name == ROCKET_CHAT_PARTIAL_UPDATE_DECISION_POC
    assert runtime_composition_for_name(composition.name).name == composition.name
    assert composition.strict_seed_cleanup
    assert composition.reader_preflight is not None
    assert composition.phase_2_admission is not None
    assert composition.phase_2_generation is not None
    assert composition.reward_evidence_loader is None
    assert composition.site_catalog.sites == ("rocketchat",)
    assert RuntimeComposition.default().seed_registry.get("theagentcompany", "rocketchat") is None
    assert not get_benchmark_capabilities("theagentcompany").supports("phase_4_execution")
