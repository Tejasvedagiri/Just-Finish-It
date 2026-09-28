"""Dev-only POC: load and unload Laya (english + typed-decisions), printing RAM at each step.

Run:  uv run --extra laya --with psutil python utils/laya_poc.py [inproc|child]

- inproc: Router() -> predict -> router.unload() -> gc, all in this process.
- child:  the same load/predict runs in a child process that exits afterwards,
          which is the only way the OS gets the memory back (see laya_plan.md §5.1.1).
"""
import gc
import multiprocessing as mp
import sys
import time

import psutil

MODELS = ("english", "typed-decisions")

QUESTIONS = {
    "verdict": {"type": "choice",
                "instructions": "Is `node` the smallest sensible unit, or does it need breaking down?",
                "criteria": {"A": "good: one function with its unit test",
                             "B": "breakdown: needs smaller pieces",
                             "C": "redo: badly designed (operational step, vague, duplicate)"}},
}
STATE = {"level": "task", "node": "implement search_todos(q: str) -> list[Todo] in app/api/todos.py"}


def rss():
    return psutil.Process().memory_info().rss / 2**30


def show(label, t0=None):
    took = "" if t0 is None else f"  ({time.perf_counter() - t0:.1f}s)"
    print(f"  {label:<38} rss={rss():.2f} GiB{took}", flush=True)


def load_and_predict():
    from laya import Router
    t0 = time.perf_counter()
    router = Router(max_loaded=len(MODELS))
    router.preload(list(MODELS))
    show(f"loaded {', '.join(MODELS)}", t0)
    for model in MODELS:
        t0 = time.perf_counter()
        answer = router.predict(STATE, QUESTIONS, model=model)["answers"]["verdict"]
        show(f"{model}: {answer['choice']} ({answer['answer_confidence']:.2f})", t0)
    return router


def child_run():
    load_and_predict()
    show("child about to exit")


def main(mode):
    show("start")
    import laya  # noqa: F401  (imports torch too)
    show("after import laya")

    if mode == "inproc":
        router = load_and_predict()
        t0 = time.perf_counter()
        router.unload()
        del router
        gc.collect()
        show("after router.unload() + gc", t0)
    else:
        t0 = time.perf_counter()
        p = mp.get_context("spawn").Process(target=child_run)
        p.start()
        p.join()
        show("parent, after child exited", t0)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "inproc"
    print(f"mode={mode}")
    main(mode)
