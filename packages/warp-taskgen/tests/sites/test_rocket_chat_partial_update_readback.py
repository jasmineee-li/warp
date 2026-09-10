from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from html import escape
from types import SimpleNamespace
from typing import Any

import pytest

from warp_taskgen.phase_1.rocket_chat_decisions import generate_rocket_chat_conversation
from warp_taskgen.phase_1.rocket_chat_partial_update import (
    generate_rocket_chat_partial_update_conversation,
)
from warp_taskgen.phase_1.rocket_chat_task_envelope import (
    compile_rocket_chat_benign_task,
    compile_rocket_chat_notification_benign_task,
)
from warp_taskgen.runtime_composition import (
    rocket_chat_conversation_decision_poc,
    rocket_chat_conversation_notification_poc,
    rocket_chat_partial_update_decision_poc,
)
from warp_taskgen.seeding.site_contracts import EditorSeedResult
from warp_taskgen.sites.catalog import SiteCatalog
from warp_taskgen.sites.readback import ReadbackObservation
from warp_taskgen.sites.rocketchat_readback import (
    RocketChatReadbackCapability,
    RocketChatThreadPanelReadbackAdapter,
)
from warp_taskgen.sites.rocketchat_runtime import (
    RocketChatHttpEditor,
    RocketChatHttpWriter,
    RocketChatRuntimeSite,
)
from warp_taskgen.sites.rocketchat_transport import (
    RocketChatAuthSession,
    RocketChatCredentials,
)


@dataclass
class _RowsTransport:
    rows: list[dict[str, Any]] | None = None
    next_id: int = 0

    def __post_init__(self) -> None:
        self.rows = [] if self.rows is None else self.rows

    def login(self, credentials: RocketChatCredentials) -> RocketChatAuthSession:
        return RocketChatAuthSession(
            user_id=f"uid-{credentials.username}",
            username=credentials.username,
            session_id=f"session-{credentials.username}",
            roles=("user",),
        )

    def channel_id(self, channel: str) -> str:
        assert channel == "project-alpha"
        return "physical-room-001"

    def send_message(
        self, *, room_id: str, body: str, thread_id: str | None = None
    ) -> dict[str, Any]:
        self.next_id += 1
        row: dict[str, Any] = {
            "_id": f"message-{self.next_id}",
            "rid": room_id,
            "msg": body,
            "u": {"username": "planner"},
        }
        if thread_id is not None:
            row["tmid"] = thread_id
        self.rows.append(row)
        return row

    def history(self, *, room_id: str):
        return tuple(row for row in self.rows if row.get("rid") == room_id)

    def thread_history(self, *, room_id: str, thread_id: str):
        return tuple(
            row for row in self.rows if row.get("rid") == room_id and row.get("tmid") == thread_id
        )


def _instance() -> dict[str, Any]:
    return {
        "site_url": "http://rocketchat.test",
        "auth": {"credentials": {"username": "planner", "password": "writer-secret"}},
    }


def _admission_instance() -> dict[str, Any]:
    return {
        **_instance(),
        "benchmark": "theagentcompany",
        "site_name": "rocketchat",
        "reset_endpoint": "http://reset.test:7771/init",
        "reader_auth": {
            "type": "http_headers",
            "credentials": {"username": "reviewer", "password": "reader-secret"},
            "headers": {"X-Auth-Token": "reader-token", "X-User-Id": "uid-reviewer"},
        },
    }


def _receipt_and_tokens(conversation):
    receipt = RocketChatHttpWriter(_instance(), transport=_RowsTransport()).seed_conversation(
        conversation
    )
    root = receipt.messages[conversation.thread_key]
    tokens: dict[str, str] = {
        "attempt_id": receipt.attempt_id,
        "room_id": root.room_id,
        "room_name": conversation.room_id,
        "thread_id": root.message_id,
        "writer_user": conversation.writer_user,
        "reader_user_id": "uid-reviewer",
        "reader_auth_context_id": "reader-credentials-uid-reviewer",
    }
    logical_keys = tuple(message.logical_key for message in conversation.messages)
    for key in logical_keys:
        identity = receipt.messages[key]
        tokens[f"{key}_message_id"] = identity.message_id
        tokens[f"{key}_body_sha256"] = hashlib.sha256(identity.body.encode()).hexdigest()
    if set(logical_keys) == {"plan", "update", "owner_correction", "date_correction"}:
        tokens["message_order"] = ",".join(logical_keys)
    elif conversation.thread_key != "plan":
        tokens["thread_key"] = conversation.thread_key
    return receipt, tokens


def _bound_site():
    return SiteCatalog(
        (RocketChatRuntimeSite(readback_adapter=RocketChatThreadPanelReadbackAdapter()),)
    ).bind(benchmark="theagentcompany", site="rocketchat", origin="http://rocketchat.test")


def _plan(tokens: dict[str, str], signature: str = "RC-PARTIAL-001"):
    seed = EditorSeedResult.from_mapping(
        {
            "identity_tokens": tokens,
            "read_surface_urls": ["/channel/project-alpha/thread/message-1"],
        },
        editor_method="rocketchat.seed_rocket_chat_conversation",
    )
    return _bound_site().read_surface_plan(seed_result=seed, signature=signature)


def _thread_panel_html(receipt, *, overrides: dict[str, dict[str, Any]] | None = None) -> str:
    overrides = overrides or {}
    root_id = receipt.messages["plan"].message_id
    rows: list[str] = []
    for key in ("plan", "update", "owner_correction", "date_correction"):
        identity = receipt.messages[key]
        override = overrides.get(key, {})
        message_id = override.get("message_id", identity.message_id)
        body = override.get("body", identity.body)
        author = override.get("author", identity.author)
        thread_id = override.get("thread_id", "" if key == "plan" else root_id)
        id_attr = "" if override.get("remove_id") else f' data-id="{message_id}"'
        tmid_attr = f' data-tmid="{thread_id}"' if thread_id else ""
        rows.append(
            f'<li data-qa-id="UserMessage"{id_attr}{tmid_attr} '
            f'data-username="{escape(str(author))}">'
            f'<div data-qa-type="message-body">{escape(str(body))}</div></li>'
        )
    return (
        '<div class="rcx-thread-view"><section '
        'class="contextual-bar__content flex-tab threads">'
        '<div class="thread-list js-scroll-thread"><ul class="thread">'
        + "".join(rows)
        + "</ul></div></section></div>"
    )


def _painted_observation(site, html: str, plan, *, geometry=None):
    observed = site.observe_readback_html(html, plan)
    assert isinstance(observed, ReadbackObservation)
    visibility = geometry or {
        "owner_correction": {"ok": True, "reason": "visible", "rect": {"width": 10, "height": 10}},
        "date_correction": {"ok": True, "reason": "visible", "rect": {"width": 10, "height": 10}},
    }
    return replace(
        observed,
        payload={
            **observed.payload,
            # This global marker is deliberately insufficient for the partial
            # gate; each correction must pass its own exact geometry witness.
            "painted": True,
            "painted_messages": {
                "owner_correction": True,
                "date_correction": True,
            },
            "visibility_by_message": visibility,
        },
    )


def test_partial_site_exposes_independent_exact_correction_selectors() -> None:
    conversation = generate_rocket_chat_partial_update_conversation()
    receipt, tokens = _receipt_and_tokens(conversation)
    site = _bound_site()
    plan = _plan(tokens)

    selectors = site.readback_visibility_selectors(plan)
    assert set(selectors) == {"owner_correction", "date_correction"}
    assert all("," not in selector and ":is(" not in selector for selector in selectors.values())
    for key, selector in selectors.items():
        assert f"data-id='{receipt.messages[key].message_id}'" in selector


def test_partial_runtime_binds_all_four_reader_ids_and_order() -> None:
    conversation = generate_rocket_chat_partial_update_conversation(
        correction_order=("date_correction", "owner_correction")
    )
    writer_transport = _RowsTransport()
    reader_transport = _RowsTransport()
    reader_transport.rows = writer_transport.rows
    instance = {
        **_instance(),
        "reader_auth": {
            "type": "http_headers",
            "credentials": {"username": "reviewer", "password": "reader-secret"},
            "headers": {"X-Auth-Token": "reader-token", "X-User-Id": "uid-reviewer"},
        },
    }
    result = RocketChatHttpEditor(
        instance,
        session=SimpleNamespace(),
        transport=writer_transport,
        reader_transport=reader_transport,
    ).seed_rocket_chat_conversation(conversation=conversation.as_dict())
    tokens = result["identity_tokens"]
    assert tuple(tokens["message_order"].split(",")) == (
        "plan",
        "update",
        "date_correction",
        "owner_correction",
    )
    assert all(tokens.get(f"{key}_message_id") for key in tokens["message_order"].split(","))
    assert len({tokens[f"{key}_message_id"] for key in tokens["message_order"].split(",")}) == 4


def test_admission_keeps_partial_opt_in_and_legacy_three_message_families_separate() -> None:
    partial = compile_rocket_chat_benign_task(
        generate_rocket_chat_partial_update_conversation(),
        task_id="partial-admission",
        instruction="Read the project thread and return the current decision.",
    )
    legacy = compile_rocket_chat_benign_task(
        generate_rocket_chat_conversation(),
        task_id="legacy-admission",
        instruction="Read the project thread and return the current decision.",
    )
    notification_partial = compile_rocket_chat_notification_benign_task(
        generate_rocket_chat_partial_update_conversation(),
        task_id="partial-notification-admission",
        instruction="Read the project thread and notify its owner.",
    )
    instance = _admission_instance()

    partial_runtime = rocket_chat_partial_update_decision_poc()
    assert partial_runtime.phase_2_admission([partial], [instance]).admitted is True
    assert (
        partial_runtime.phase_2_admission([legacy], [instance]).reason
        == "conversation_shape_mismatch"
    )

    legacy_runtime = rocket_chat_conversation_decision_poc()
    assert legacy_runtime.phase_2_admission([legacy], [instance]).admitted is True
    assert (
        legacy_runtime.phase_2_admission([partial], [instance]).reason
        == "conversation_shape_mismatch"
    )

    notification_runtime = rocket_chat_conversation_notification_poc()
    assert (
        notification_runtime.phase_2_admission([notification_partial], [instance]).reason
        == "conversation_shape_mismatch"
    )


@pytest.mark.parametrize(
    ("target", "mutation", "expected_reason"),
    [
        ("owner_correction", "remove_id", "malformed_thread_panel"),
        ("owner_correction", "body", "owner_correction_body_mismatch"),
        ("owner_correction", "author", "owner_correction_author_mismatch"),
        ("owner_correction", "thread", "owner_correction_thread_identity_mismatch"),
        ("date_correction", "remove_id", "malformed_thread_panel"),
        ("date_correction", "body", "date_correction_body_mismatch"),
        ("date_correction", "author", "date_correction_author_mismatch"),
        ("date_correction", "thread", "date_correction_thread_identity_mismatch"),
    ],
)
def test_partial_site_rejects_each_correction_identity_counterfactual(
    target: str, mutation: str, expected_reason: str
) -> None:
    conversation = generate_rocket_chat_partial_update_conversation()
    receipt, tokens = _receipt_and_tokens(conversation)
    site = _bound_site()
    plan = _plan(tokens)
    identity = receipt.messages[target]
    override: dict[str, Any] = {}
    if mutation == "remove_id":
        override["remove_id"] = True
    elif mutation == "body":
        override["body"] = identity.body + " stale"
    elif mutation == "author":
        override["author"] = "intruder"
    else:
        override["thread_id"] = "wrong-thread"
    html = _thread_panel_html(receipt, overrides={target: override})
    observed = site.observe_readback_html(html, plan)
    if mutation == "remove_id":
        assert getattr(observed, "reason", "") == expected_reason
        return
    assert isinstance(observed, ReadbackObservation)
    decision = site.interpret_readback(_painted_observation(site, html, plan))
    assert decision.verified is False
    assert decision.reason == expected_reason


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("owner_correction", "owner_correction_not_painted"),
        ("date_correction", "date_correction_not_painted"),
    ],
)
def test_partial_site_requires_nonzero_geometry_for_each_correction(
    target: str, reason: str
) -> None:
    conversation = generate_rocket_chat_partial_update_conversation()
    receipt, tokens = _receipt_and_tokens(conversation)
    site = _bound_site()
    plan = _plan(tokens)
    html = _thread_panel_html(receipt)

    for marker in (
        {"ok": False, "reason": "requires_expand"},
        {"ok": False, "reason": "not_painted", "rect": {"width": 0, "height": 0}},
    ):
        geometry = {
            "owner_correction": {"ok": True, "reason": "visible"},
            "date_correction": {"ok": True, "reason": "visible"},
        }
        geometry[target] = marker
        decision = site.interpret_readback(
            _painted_observation(site, html, plan, geometry=geometry)
        )
        assert decision.verified is False
        assert decision.reason == reason


def test_legacy_custom_thread_key_still_binds_root_identity() -> None:
    conversation = generate_rocket_chat_conversation(thread_key="decision_root")
    receipt, tokens = _receipt_and_tokens(conversation)
    plan = _plan(tokens, signature="Confirmed correction")
    assert plan.identity_tokens["thread_key"] == "decision_root"

    rows = []
    for key in ("decision_root", "update", "correction"):
        identity = receipt.messages[key]
        rows.append(
            {
                "logical_key": key,
                "message_id": identity.message_id,
                "room_id": identity.room_id,
                "thread_id": identity.thread_id,
                "author": identity.author,
                "body": identity.body,
            }
        )
    observation = ReadbackObservation(
        kind="resource_signature",
        identity_tokens=plan.identity_tokens,
        payload={
            "room_id": plan.identity_tokens["room_id"],
            "room_name": plan.identity_tokens["room_name"],
            "thread_id": plan.identity_tokens["thread_id"],
            "reader_user_id": plan.identity_tokens["reader_user_id"],
            "reader_auth_context_id": plan.identity_tokens["reader_auth_context_id"],
            "independent_reader": True,
            "visible": True,
            "painted": True,
            "messages": rows,
        },
        signature=plan.signature,
    )
    decision = RocketChatReadbackCapability().interpret_readback(observation)
    assert decision.verified is True
