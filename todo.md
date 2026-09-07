# TODO — JFI

- [ ] **Test with other models**
  - Smoke-test each candidate (set `MODEL` in `.env`, short conversation through the runner). *Status: only the current model (`qwen3.8-27b-ultra…`) was smoke-tested and passed; alternatives were not loaded at test time ("Failed to load model" — server may keep one model resident). Deeper testing deferred per user request.*
  - Run the `evals/csv2md` harness against each candidate and record pass/fail + latency.

- [ ] **Rename the LLM manager to OpenAI**
  - Rename `src/JFI/llm/colibri_llm_stream.py` → `openai_llm_stream.py`; class `ColibriLLMStream` → `OpenAILLMStream`.
  - Update references in `src/JFI/runner.py`, `test/test_themes.py`, and any docstrings/comments.

- [ ] **Make the tool-call cmd safer + approval on the cmd name**
  - Harden `execute_command()` in `src/JFI/tool/cmd_tools.py` (shlex parsing, empty/metacharacter guards, keep timeout) with unit tests.
  - Gate each command behind user approval before execution.
  - Add the cmd tool name (`execute_command`) to the project's approval list where that lives (no dedicated list exists yet — README's "The tool set" table is the natural home).

- [ ] **Run some benchmarks**
  - Run `evals/csv2md` (`drive.py` / `drive_pty.py`) with the current model; capture stdout/logs.
  - Compare against at least one alternative model if available.
  - Record results (pass/fail per task, wall time) in `todo.md` or `evals/RESULTS.md`.

- [ ] **Update the README**
  - Reflect the OpenAI LLM rename, safer cmd tool + its approval requirement, benchmark results, and model-testing notes in `README.md`.
