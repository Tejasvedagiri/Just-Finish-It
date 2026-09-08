"""End-to-end coverage for runner.main()'s per-phase LLM construction:

    llms = {phase: OpenAICompatableStream(prefix) for phase, prefix in PHASE_ENV_PREFIX.items()}

phase_env() itself (the {PREFIX}_{KEY}-with-fallback resolution) is unit-
tested elsewhere, but nothing previously exercised main() actually wiring
PHASE_ENV_PREFIX through to one real OpenAICompatableStream per phase --
this pins that a phase with its own .env override gets its own model/
endpoint, and a phase without one falls back to the shared default,
through the real construction path.
"""


def test_main_builds_one_llm_stream_per_phase_honoring_overrides(monkeypatch):
    import JFI.manager.pt_console_manager as ptm
    import JFI.runner as runner

    monkeypatch.setenv("OPENAI_URL", "http://shared.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "shared-key")
    monkeypatch.setenv("MODEL", "shared-model")
    monkeypatch.setenv("TEMPERATURE", "0.7")

    # Only the reviewer phase gets a per-phase override -- planner/imp/testing
    # must all fall back to the shared settings above.
    monkeypatch.setenv("REVIEWER_MODEL", "reviewer-model")
    monkeypatch.setenv("REVIEWER_OPENAI_URL", "http://reviewer.example/v1")
    monkeypatch.setenv("REVIEWER_OPENAI_API_KEY", "reviewer-key")
    monkeypatch.setenv("REVIEWER_TEMPERATURE", "0.2")
    for prefix in ("PLANNER", "IMP", "TESTING"):
        for key in ("MODEL", "OPENAI_URL", "OPENAI_API_KEY", "TEMPERATURE"):
            monkeypatch.delenv(f"{prefix}_{key}", raising=False)

    monkeypatch.setattr(runner, "load_dotenv", lambda *a, **k: True)
    monkeypatch.setattr(runner, "find_dotenv", lambda *a, **k: ".env")

    def spy_init(self, *args, **kwargs):
        self.run = lambda fn: fn()  # actually invoke the worker, unlike a no-op stub
        self.dump_transcript = lambda: None
        self.clear_console = lambda: None

    monkeypatch.setattr(ptm.PromptToolkitConsoleManager, "__init__", spy_init)

    captured = {}

    def fake_run_pipeline(console, llms):
        captured["llms"] = llms

    monkeypatch.setattr(runner, "run_pipeline", fake_run_pipeline)

    runner.main()

    llms = captured["llms"]
    assert set(llms.keys()) == set(runner.PHASES) == set(runner.PHASE_ENV_PREFIX.keys())

    reviewer = llms["reviewer"]
    assert reviewer.model == "reviewer-model"
    assert reviewer.temperature == "0.2"
    assert reviewer.stream_service.base_url.host == "reviewer.example"

    for phase in ("planner", "imp", "testing"):
        stream = llms[phase]
        assert stream.model == "shared-model"
        assert stream.temperature == "0.7"
        assert stream.stream_service.base_url.host == "shared.example"

    # Every phase got its own instance -- not four names pointing at one
    # shared object, which would silently defeat per-phase routing.
    assert len({id(s) for s in llms.values()}) == 4
