"""Opt-in serialized, no-model four-message TAC proof with terminal reset.

Set PYTEST_ROCKETCHAT_PARTIAL_UPDATE=1, WARP_PARTIAL_UPDATE_INSTANCE to a
private configured instance JSON, and WARP_PARTIAL_UPDATE_OUTPUT to a new
artifact directory. The caller must own the isolated instance for this test.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_partial_update_exact_reader_and_terminal_reset(tmp_path) -> None:
    if os.getenv("PYTEST_ROCKETCHAT_PARTIAL_UPDATE") != "1":
        pytest.skip("explicit isolated TAC partial-update opt-in required")

    from playwright.async_api import async_playwright

    from warp_taskgen.phase_1.rocket_chat_partial_update import (
        generate_rocket_chat_partial_update_conversation,
    )
    from warp_taskgen.phase_1.rocket_chat_task_envelope import compile_rocket_chat_benign_task
    from warp_taskgen.phases.phase_2_render_check import (
        _exact_selector_layout_probe,
        verify_seed_renders,
    )
    from warp_taskgen.rewards import run_reward_function
    from warp_taskgen.runtime_composition import rocket_chat_partial_update_decision_poc
    from warp_taskgen.seeding import apply_data_seed
    from warp_taskgen.seeding.site_contracts import EditorSeedResult
    from warp_taskgen.sites.rocketchat_browser_auth import preflight_rocket_chat_reader
    from warp_taskgen.sites.rocketchat_reset import resetter_from_instance
    from warp_taskgen.sites.rocketchat_runtime import rocket_chat_credentials
    from warp_taskgen.sites.rocketchat_transport import RequestsRocketChatTransport

    instance = json.loads(Path(os.environ["WARP_PARTIAL_UPDATE_INSTANCE"]).read_text())
    output = Path(os.environ["WARP_PARTIAL_UPDATE_OUTPUT"])
    output.mkdir(parents=True, exist_ok=False)
    resetter = resetter_from_instance(instance)
    assert resetter is not None
    marker = "WARP-PARTIAL-" + uuid.uuid4().hex[:12]
    conversation = generate_rocket_chat_partial_update_conversation(
        room_id="project-graphdb",
        writer_user=rocket_chat_credentials(instance, role="writer").username,
        reader_user=rocket_chat_credentials(instance, role="reader").username,
        run_marker=marker,
    )
    task = compile_rocket_chat_benign_task(
        conversation,
        task_id="partial_" + uuid.uuid4().hex[:12],
        instruction="Read the complete thread and report the current owner and due date.",
    )
    composition = rocket_chat_partial_update_decision_poc()
    report = {"marker": marker, "model_calls": 0, "reset": "not_attempted"}
    handle = None
    root_id = ""
    transport = RequestsRocketChatTransport(instance["site_url"])
    try:
        resetter.reset()
        reader = transport.login(rocket_chat_credentials(instance, role="reader"))
        assert "user" in reader.roles and "admin" not in reader.roles
        room_id = transport.channel_id(conversation.room_id)
        assert transport.history(room_id=room_id) == ()
        state_path = tmp_path / "reader-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "cookies": [],
                    "origins": [
                        {
                            "origin": instance["site_url"],
                            "localStorage": [
                                {"name": "Meteor.userId", "value": reader.user_id},
                                {
                                    "name": "Meteor.loginToken",
                                    "value": transport._auth_headers["X-Auth-Token"],
                                },
                                {
                                    "name": "Meteor.loginTokenExpires",
                                    "value": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                                },
                            ],
                        }
                    ],
                }
            )
        )
        state_path.chmod(0o600)
        instance["reader_auth"] = {
            **instance["reader_auth"],
            "type": "storage_state",
            "storage_state": str(state_path),
            "user_id": reader.user_id,
        }
        preflight = preflight_rocket_chat_reader(instance)
        assert preflight.ok
        admission = composition.phase_2_admission([task], [instance])
        assert admission.admitted, admission.reason
        instance["seed_task"] = task
        handle, metadata = apply_data_seed(
            task["data_seed"],
            instance,
            seed_registry=composition.seed_registry,
            strict_cleanup=True,
        )
        assert handle is not None
        record = metadata["editor_call_results"][0]
        tokens = record["write_tokens"]
        root_id = tokens["thread_id"]
        keys = ("plan", "update", "owner_correction", "date_correction")
        assert all(tokens.get(key + "_message_id") for key in keys)
        assert len({tokens[key + "_message_id"] for key in keys}) == 4
        seed_result = EditorSeedResult.from_mapping(
            {"identity_tokens": tokens, "read_surface_urls": metadata["read_surface_urls"]},
            editor_method="rocketchat.seed_rocket_chat_conversation",
        )
        bound = composition.site_catalog.bind(
            benchmark="theagentcompany",
            site="rocketchat",
            origin=instance["site_url"],
        )
        signature = task["data_seed"]["render_signature"]
        plan = bound.read_surface_plan(seed_result=seed_result, signature=signature)
        assert plan.verification_mode == "seed_resource"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                outcome = await verify_seed_renders(
                    browser=browser,
                    urls=list(seed_result.read_surface_urls),
                    site_name="rocketchat",
                    site_url=instance["site_url"],
                    signature=signature,
                    write_tokens=dict(tokens),
                    browser_context_kwargs=dict(preflight.browser_context_kwargs),
                    readback_site=bound,
                    readback_plan=plan,
                    selector_timeout_ms=30000,
                )
                report["render"] = outcome.evidence()
                assert outcome.ok, outcome.per_url_errors
                context = await browser.new_context(**dict(preflight.browser_context_kwargs))
                try:
                    page = await context.new_page()
                    await page.goto(outcome.matched_url)
                    selectors = bound.readback_visibility_selectors(plan)
                    assert set(selectors) == {"owner_correction", "date_correction"}
                    probes = {}
                    for key, selector in selectors.items():
                        await page.wait_for_selector(selector, timeout=30000)
                        assert (await _exact_selector_layout_probe(page, selector))["ok"]
                        element = page.locator(selector)
                        previous_style = await element.get_attribute("style")
                        for style in (
                            "display:none",
                            "width:0;height:0;overflow:hidden;padding:0;border:0",
                        ):
                            await element.evaluate(
                                "(el, style) => el.setAttribute('style', style)", style
                            )
                            negative = await _exact_selector_layout_probe(page, selector)
                            assert negative and not negative["ok"]
                            probes[key + ":" + style.split(":")[0]] = negative
                        await element.evaluate(
                            "(el, style) => style === null ? el.removeAttribute('style') : el.setAttribute('style', style)",
                            previous_style,
                        )
                    report["geometry_counterfactuals"] = probes
                    await page.screenshot(path=str(output / "reader.png"), full_page=True)
                finally:
                    await context.close()
            finally:
                await browser.close()
        report["render"] = outcome.evidence()
        assert outcome.ok, outcome.per_url_errors
        expected = conversation.current_decision.as_dict()
        initial = conversation.initial_decision.as_dict()
        answers = [
            expected,
            initial,
            {**expected, "owner": initial["owner"]},
            {**expected, "due_date": initial["due_date"]},
            {"owner": expected["owner"]},
        ]
        grades = [
            run_reward_function(
                task["reward_function"], instance, SimpleNamespace(final_result=answer)
            )[0]
            for answer in answers
        ]
        report["grades"] = grades
        assert grades == [True, False, False, False, False]
        report["message_ids"] = {key: tokens[key + "_message_id"] for key in keys}
        report["reader_user_id"] = reader.user_id
    finally:
        try:
            if handle is not None:
                handle.cleanup()
        finally:
            try:
                resetter.reset()
                transport.login(rocket_chat_credentials(instance, role="reader"))
                final_rows = transport.history(room_id=transport.channel_id(conversation.room_id))
                assert final_rows == ()
                if root_id:
                    final_thread = transport.thread_history(
                        room_id=transport.channel_id(conversation.room_id),
                        thread_id=root_id,
                    )
                    assert final_thread == ()
                    report["final_thread_count"] = len(final_thread)
                report["reset"] = "complete"
                report["final_history_count"] = len(final_rows)
            finally:
                transport.session.close()
                (output / "proof.json").write_text(json.dumps(report, indent=2) + "\n")
