"""Rewrite the training data's wording with a local LLM (LM Studio), in place,
so the judge learns the concepts rather than one author's phrasing.

Why: the first fine-tunes scored 100% on the held-out set but only 54% on a
stress set written to break the data's surface patterns (every BREAKDOWN
ending in "...: a, b, c and d", every REDO a short command, every GOOD
"implement f(x) in file.py"), and every node of an app repeated the same
goal word for word -- the benchmark ones being long prompts that the judge
cuts at 300 characters, so dozens of rows showed Laya an identical goal.

For every original row, one request (batched) asks for, in a random style:
  - goal_a: a new wording of its goal -> replaces the row's goal in place;
  - goal_b, node, done_when: a paraphrased copy -> appended as a new row
    (source + "+para", `style` tag).
Meaning, scope and so the label never change (see SYSTEM). Every row gets an
`app_id` (from its ORIGINAL goal) first, so finetune.py can still hold out
whole apps once the goals no longer match.

    uv run python laya-finetuning/data/paraphrase.py --file laya-finetuning/data/train.jsonl
    cp laya-finetuning/data/test.jsonl laya-finetuning/data/test_para.jsonl
    uv run python laya-finetuning/data/paraphrase.py --file laya-finetuning/data/test_para.jsonl

No two rows end up with the same goal: rewrites that repeat any goal
already in the file are rejected and retried on the next run.

The file is rewritten atomically after every batch; rerunning the same
command skips rows already done, so an interrupted run just resumes.
Endpoint: LM Studio's OpenAI-compatible API, i.e. the same request as
    curl http://127.0.0.1:1234/v1/chat/completions -H "Content-Type: application/json" -d @req.json
"""
import argparse
import hashlib
import json
import os
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

STYLES = [
    "a terse ticket title of 4 to 10 words; leave out the file path",
    "one long, wordy sentence that adds harmless background context",
    "a casual chat message from a teammate",
    "a formal requirement starting with 'The system shall' or 'Ensure that'",
    "a question, e.g. 'Can we ...?' or 'Could you ...?'",
    "summarise any list of items into one short umbrella phrase instead of listing them",
    "put the file or area first, then what to do with it",
    "passive voice",
    "a user story ('As a developer, I want ...')",
    "plain words for a non-expert, avoiding jargon where possible",
]

SYSTEM = """You rewrite a software dataset's wording in requested writing styles.

Each input item has a project goal, a plan item (node) and its finish condition (done_when). Return:
- goal_a: the SAME project described in new words, in the item's style.
- goal_b: the SAME project described again, worded differently from both the input and goal_a.
- node: the plan item rewritten in the item's style.
- done_when: the finish condition rewritten in the same style ("" if the input's is empty).

Hard rules:
- Goals: same product, same language/stack and same requirements, 1-2 sentences, under 250 characters. Never copy the input goal's wording.
- Every goal_a and goal_b in your reply must be worded differently from every other goal in the reply, even for items that share the same input goal.
- Node: keep EXACTLY the same amount and kind of work. Never add, remove, split or merge work. A big multi-part job must still read as big; a single small change must still read as single and small.
- If the node is an action to operate something (install, run, start, stop, open, view, deploy, test-run, git), it must stay an action of operating something, not become building code.
- If the node is vague (no concrete file, function or result), keep it vague; do not invent specifics.
- Keep file, function and command names when you keep them at all; never invent new ones.
- Node under 200 characters.

Reply with ONLY a JSON array, one object per input item, in the same order:
[{"i": <index>, "goal_a": "...", "goal_b": "...", "node": "...", "done_when": "..."}]"""


def app_id(goal):
    return hashlib.sha1(goal.encode("utf-8")).hexdigest()[:10]


def extract_array(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise ValueError("no JSON array in the reply")
    return json.loads(text[start:end + 1])


def request(client, url, model, batch):
    """batch: list of (index into rows, row, style). Returns {index: reply object}."""
    items = [{"i": k, "style": style, "goal": row["goal"], "node": row["node"],
              "done_when": row.get("done_when", "")} for k, (_, row, style) in enumerate(batch)]
    user = "Rewrite each item in its own style:\n" + json.dumps(items, ensure_ascii=False, indent=1) + "\n/no_think"
    resp = client.post(f"{url}/chat/completions", json={
        # Reasoning models spend most of this thinking (observed: 1,362 of
        # 1,582 tokens for two items, /no_think ignored); a 6,000 cap cut the
        # JSON off mid-array at 6 items per batch.
        "model": model, "temperature": 0.9, "max_tokens": 16000,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
    })
    resp.raise_for_status()
    by_k = {o["i"]: o for o in extract_array(resp.json()["choices"][0]["message"]["content"])
            if isinstance(o, dict) and "i" in o}
    return {batch[k][0]: by_k[k] for k in range(len(batch)) if k in by_k}


def valid(o, row, used_goals):
    """`used_goals`: every goal already in the file -- a repeat is rejected
    (and retried on the next run) so no two rows end up sharing a goal."""
    goal_a, goal_b, node = (o.get(k, "").strip() for k in ("goal_a", "goal_b", "node"))
    return (goal_a and goal_b and node and len(node) <= 220 and len(goal_a) <= 320 and len(goal_b) <= 320
            and goal_a != goal_b and goal_a != row["goal"] and node.lower() != row["node"].lower()
            and goal_a.lower() not in used_goals and goal_b.lower() not in used_goals)


def save(path, rows):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="JSONL rewritten in place")
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--workers", type=int, default=2, help="parallel requests; match the model's parallel slots")
    ap.add_argument("--url", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--model", default=None, help="default: the first model LM Studio lists")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    path = Path(args.file)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in rows:
        r.setdefault("app_id", app_id(r["goal"]))  # before any goal is rewritten
    rng = random.Random(args.seed)
    pending = [(i, r) for i, r in enumerate(rows)
               if "+para" not in str(r.get("source", "")) and not r.get("rewritten")]
    # Rows of one app sit next to each other and share a goal; distinct
    # styles within a batch keep their rewritten goals from coming out
    # identical (observed: same goal + same style -> the same goal_a).
    todo = []
    for start in range(0, len(pending), args.batch):
        chunk = pending[start:start + args.batch]
        for (i, r), style in zip(chunk, rng.sample(STYLES, len(chunk))):
            todo.append((i, r, style))
    print(f"{path.name}: {len(rows)} rows, {len(todo)} originals still to rewrite")
    save(path, rows)
    if not todo:
        return

    with httpx.Client(timeout=1800) as client:
        model = args.model or client.get(f"{args.url}/models").json()["data"][0]["id"]
        print(f"model: {model}", flush=True)
        batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]
        used_goals = {r["goal"].strip().lower() for r in rows}
        done = failed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(request, client, args.url, model, b): b for b in batches}
            for n, fut in enumerate(as_completed(futures), 1):
                batch = futures[fut]
                try:
                    replies = fut.result()
                except Exception as e:
                    failed += len(batch)
                    print(f"  batch failed ({type(e).__name__}: {e}); rerun to retry", file=sys.stderr, flush=True)
                    continue
                for idx, row, style in batch:
                    o = replies.get(idx)
                    if not o or not valid(o, row, used_goals):
                        failed += 1
                        continue
                    used_goals.update({o["goal_a"].strip().lower(), o["goal_b"].strip().lower()})
                    new_row = {**row, "goal": o["goal_b"].strip(), "node": o["node"].strip(),
                               "done_when": o.get("done_when", "").strip() if row.get("done_when") else "",
                               "source": f"{row.get('source', 'handwritten')}+para", "style": style,
                               "rewritten": True}
                    rows[idx] = {**row, "goal": o["goal_a"].strip(), "goal_style": style, "rewritten": True}
                    rows.append(new_row)
                    done += 1
                save(path, rows)
                print(f"  {n}/{len(batches)} batches, {done} originals rewritten", flush=True)
    print(f"done: {done} rewritten, {failed} failed (rerun the same command to retry the rest)")


if __name__ == "__main__":
    main()
