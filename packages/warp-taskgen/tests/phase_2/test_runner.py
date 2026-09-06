# ruff: noqa
# Auto-split from tests/test_phase_2_injections.py; shared helpers live in tests/phase_2/_fixtures.py.
from ._fixtures import *  # noqa: F403,F401
from warp_taskgen.phase_2 import generation
from warp_taskgen.phase_2 import runner as phase_2_injections
from warp_taskgen.phase_2 import target_inputs


@pytest.mark.asyncio
async def test_phase_2_run_publishes_partial_results_on_partial_site_failures(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("WORLDSIM_STATE_DIR", str(tmp_path))
    (tmp_path / "phase_1").mkdir(parents=True)
    (tmp_path / "phase_1" / "benign_tasks.json").write_text(
        json.dumps(
            [
                _benign_task(),
                {
                    **_benign_task(),
                    "id": "benign-2",
                    "site": "gitlab",
                    "sites": ["gitlab"],
                    "start_urls": ["__GITLAB__/issues"],
                },
            ]
        )
    )
    (tmp_path / "phase_0c").mkdir(parents=True)
    profile_payload = json.dumps(
        {
            "data_model": [],
            "injection_surface": [],
            "verification_capabilities": [],
        }
    )
    (tmp_path / "phase_0c" / "BENCHMARK_PROFILE_shopping.json").write_text(profile_payload)
    (tmp_path / "phase_0c" / "BENCHMARK_PROFILE_gitlab.json").write_text(profile_payload)

    async def fake_generate(
        site_name, site_tasks, all_site_tasks=None, profile_path=None, label=None, **kwargs
    ):
        if site_name == "shopping":
            return SiteInjectionResult(
                site_name,
                [{"id": "adv-1", "benchmark": "webarena_verified"}],
                [],
            )
        return SiteInjectionResult(
            site_name,
            [],
            ["sandbox did not produce adversarial_tasks.json"],
        )

    monkeypatch.setattr(generation, "_generate_injections_for_site", fake_generate)

    rc = await phase_2_injections.run(Namespace(skip_feasibility=True, sandbox_model="demo"))

    assert rc == 0
    output_path = tmp_path / "phase_2" / "adversarial_tasks.json"
    assert output_path.exists()
    assert _strip_feasibility(json.loads(output_path.read_text())) == [
        {"id": "adv-1", "benchmark": "webarena_verified"}
    ]
    state = json.loads((tmp_path / "pipeline_state.json").read_text())
    assert state["status"] == "partial_complete"
    assert state["partial"] is True
    assert state["generation_failures"] == [
        "gitlab: sandbox did not produce adversarial_tasks.json"
    ]
    assert "runtime_composition" not in state


@pytest.mark.asyncio
async def test_named_rocket_chat_composition_opens_normal_phase_2_generation_gate(
    monkeypatch, tmp_path
):
    from warp_taskgen.phase_1.rocket_chat_decisions import generate_rocket_chat_conversation
    from warp_taskgen.phase_1.rocket_chat_task_envelope import compile_rocket_chat_benign_task
    from warp_taskgen.runtime_composition import ROCKET_CHAT_CONVERSATION_DECISION_POC

    monkeypatch.setenv("WORLDSIM_STATE_DIR", str(tmp_path))
    (tmp_path / "phase_1").mkdir(parents=True)
    benign = compile_rocket_chat_benign_task(
        generate_rocket_chat_conversation(),
        task_id="novel_rocketchat_1",
        instruction="Read the complete thread and return the current owner and due date.",
    )
    (tmp_path / "phase_1" / "benign_tasks.json").write_text(json.dumps([benign]))
    (tmp_path / "phase_0c").mkdir(parents=True)
    (tmp_path / "phase_0c" / "BENCHMARK_PROFILE_rocketchat.json").write_text(
        json.dumps(
            {
                "data_model": [],
                "injection_surface": [],
                "verification_capabilities": [],
            }
        )
    )
    captured = {}

    async def fake_generate(site_name, site_tasks, **kwargs):
        captured["composition"] = kwargs.get("runtime_composition")
        return SiteInjectionResult(
            site_name,
            [{"id": "adv-rc-1", "benchmark": "theagentcompany"}],
            [],
        )

    monkeypatch.setattr(generation, "_generate_injections_for_site", fake_generate)

    rc = await phase_2_injections.run(
        Namespace(
            skip_feasibility=True,
            sandbox_model="demo",
            runtime_composition=ROCKET_CHAT_CONVERSATION_DECISION_POC,
        )
    )

    assert rc == 0
    assert captured["composition"].name == ROCKET_CHAT_CONVERSATION_DECISION_POC


@pytest.mark.asyncio
async def test_paused_run_reuses_exact_single_shard_before_api_admission(
    monkeypatch,
    tmp_path,
):
    from warp_taskgen.phase_2.pause_control import write_planning_shard_checkpoint
    from warp_taskgen.run_transition import resolve_run_request
    from warp_taskgen.state import bind_run_definition

    monkeypatch.setenv("WARP_TASKGEN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("WORLDSIM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("WARP_TASKGEN_RESUME_POINTER", str(tmp_path / "pointer.json"))
    (tmp_path / "phase_1").mkdir(parents=True)
    (tmp_path / "phase_1" / "benign_tasks.json").write_text(json.dumps([_benign_task()]))
    (tmp_path / "phase_0c").mkdir(parents=True)
    (tmp_path / "phase_0c" / "BENCHMARK_PROFILE_shopping.json").write_text(
        json.dumps(
            {
                "data_model": [],
                "injection_surface": [],
                "verification_capabilities": [],
            }
        )
    )
    definition = resolve_run_request(
        {"sandbox_model": "demo"},
        existing_state=None,
        new_run_id="run-phase2-resume",
    ).definition
    state = {
        "step": "phase_2",
        "status": "paused",
        "logs_dir": str(tmp_path),
        "sandbox_model": "demo",
        "phase_2_stage": "planning",
        "run_definition": definition.to_dict(),
    }
    (tmp_path / "pipeline_state.json").write_text(json.dumps(state))
    checkpoint = tmp_path / "phase_2" / "shards" / "shopping.json"
    plan = {"id": "adv-reused", "site": "shopping", "benchmark": "webarena_verified"}
    write_planning_shard_checkpoint(
        checkpoint,
        [plan],
        label="shopping",
        input_task_ids=["benign-1"],
    )

    async def unexpected_api_call(*args, **kwargs):
        raise AssertionError("an exact paused shard must be reused before API admission")

    monkeypatch.setattr(
        generation,
        "_generate_injections_for_site",
        unexpected_api_call,
    )

    with bind_run_definition(definition, state_dir=tmp_path):
        rc = await phase_2_injections.run(Namespace(skip_feasibility=True, sandbox_model="demo"))

    assert rc == 0
    output = json.loads((tmp_path / "phase_2" / "adversarial_tasks.json").read_text())
    assert [task["id"] for task in output] == ["adv-reused"]


@pytest.mark.asyncio
async def test_text_fill_pause_wins_zero_success_terminal_race(monkeypatch, tmp_path):
    from warp_taskgen.run_control import PauseBoundaryReached, request_pause
    from warp_taskgen.run_transition import resolve_run_request
    from warp_taskgen.state import bind_run_definition

    monkeypatch.setenv("WARP_TASKGEN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("WORLDSIM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("WARP_TASKGEN_RESUME_POINTER", str(tmp_path / "pointer.json"))
    (tmp_path / "phase_1").mkdir(parents=True)
    (tmp_path / "phase_0c").mkdir(parents=True)
    (tmp_path / "phase_2").mkdir(parents=True)
    (tmp_path / "phase_1" / "benign_tasks.json").write_text(json.dumps([_benign_task()]))
    (tmp_path / "phase_0c" / "BENCHMARK_PROFILE_shopping.json").write_text(
        json.dumps(_single_surface_profile())
    )
    (tmp_path / "phase_2" / "adversarial_plans.json").write_text(json.dumps([_plan_task()]))
    args = Namespace(skip_feasibility=True, sandbox_model="demo")
    definition = resolve_run_request(
        {"sandbox_model": "demo"},
        existing_state=None,
        new_run_id="run-zero-success-pause-race",
    ).definition

    async def unsuccessful_fill(*args, **kwargs):
        del args, kwargs
        return [], [{"task_id": "adv-1", "status": "text_unrecoverable"}]

    monkeypatch.setattr(phase_2_injections, "fill_texts_for_tasks", unsuccessful_fill)
    original_write = phase_2_injections.write_json_atomic
    diagnostics_path = tmp_path / "phase_2" / "text_fill_diagnostics.json"

    def request_pause_after_scheduler_drain(path, payload, **kwargs):
        original_write(path, payload, **kwargs)
        if path == diagnostics_path:
            request_pause(tmp_path)

    monkeypatch.setattr(
        phase_2_injections,
        "write_json_atomic",
        request_pause_after_scheduler_drain,
    )

    with bind_run_definition(definition, state_dir=tmp_path):
        save_state(
            "phase_2",
            status="running",
            phase_2_stage="text_fill",
            sandbox_model="demo",
            phase_2a_resolution_signature=target_inputs._phase_2a_resolution_signature(args),
        )
        with pytest.raises(PauseBoundaryReached):
            await phase_2_injections.run(args)

    state = json.loads((tmp_path / "pipeline_state.json").read_text())
    assert state["status"] == "running"
    assert state["phase_2_stage"] == "text_fill"
    assert (tmp_path / "pause_request.json").exists()
    assert not (tmp_path / "phase_2" / "adversarial_tasks.json").exists()


@pytest.mark.asyncio
async def test_default_composition_leaves_pipeline_state_bytes_unchanged(monkeypatch, tmp_path):
    """An unnamed Run and an explicit ``default`` Run write identical state.

    Every Run now resolves a Runtime Composition, but the default one is not
    recorded, so ``pipeline_state.json`` is byte-identical to what a
    pre-composition Run wrote.
    """

    async def _run_once(state_dir, runtime_composition):
        monkeypatch.setenv("WORLDSIM_STATE_DIR", str(state_dir))
        (state_dir / "phase_1").mkdir(parents=True)
        (state_dir / "phase_1" / "benign_tasks.json").write_text(json.dumps([_benign_task()]))
        (state_dir / "phase_0c").mkdir(parents=True)
        (state_dir / "phase_0c" / "BENCHMARK_PROFILE_shopping.json").write_text(
            json.dumps({"data_model": [], "injection_surface": [], "verification_capabilities": []})
        )

        async def fake_generate(site_name, site_tasks, **kwargs):
            return SiteInjectionResult(
                site_name,
                [{"id": "adv-1", "benchmark": "webarena_verified"}],
                [],
            )

        monkeypatch.setattr(generation, "_generate_injections_for_site", fake_generate)
        args = Namespace(skip_feasibility=True, sandbox_model="demo")
        if runtime_composition is not None:
            args.runtime_composition = runtime_composition
        rc = await phase_2_injections.run(args)
        assert rc == 0
        return (state_dir / "pipeline_state.json").read_bytes()

    unnamed_dir = tmp_path / "unnamed"
    explicit_dir = tmp_path / "explicit"
    unnamed = await _run_once(unnamed_dir, None)
    explicit_default = await _run_once(explicit_dir, "default")

    # Normalize the only things that always differ between two runs of the
    # same pipeline: the state directory each one wrote into, and the clock.
    # Every second-granularity clock field the pipeline stamps (``updated_at``,
    # ``timestamp``, ``feasibility_completed_at``, ...) can straddle a second
    # boundary between the two runs, so the comparison keeps the key set and
    # blanks every ISO timestamp value instead of naming the fields.
    _iso_timestamp = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$"
    )

    def _blank_clocks(value):
        if isinstance(value, dict):
            return {key: _blank_clocks(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_blank_clocks(item) for item in value]
        if isinstance(value, str) and _iso_timestamp.match(value):
            return "<CLOCK>"
        return value

    def _stable(raw, state_dir):
        text = raw.decode().replace(str(state_dir), "<STATE_DIR>")
        return _blank_clocks(json.loads(text))

    assert _stable(unnamed, unnamed_dir) == _stable(explicit_default, explicit_dir)
    assert b"runtime_composition" not in unnamed
    assert b"runtime_composition" not in explicit_default
