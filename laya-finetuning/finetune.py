"""Fine-tune a Laya checkpoint as JFI's plan judge (head only, CPU-friendly).

    uv run --extra laya python laya-finetuning/finetune.py --base english
    uv run --extra laya python laya-finetuning/finetune.py --base typed-decisions

Trains on data/train.jsonl only (data/test.jsonl is never read here). Every
row becomes exactly the request JFI.planner.judge sends at run time --
build_state() + questions_for(level) -- so the model learns the real
questions, not a lookalike. The encoder is frozen and only the decision head
(head layers, type embedding, scorer) is trained with soft cross-entropy --
the supervised half of Laya's RLCD recipe; the policy-gradient half and the
2-GPU schedule of the upstream notebook are overkill for ~140 rows on CPU.

Output: checkpoints/<name>/ in Laya's normal layout (rl_agent_config.json,
model.safetensors, tokenizer/, encoder/), loadable with
Router(models={"english": <dir>}) -- see LAYA_ENGLISH_PATH in the judge.
"""
import argparse
import json
import random
import shutil
import sys
import time
from pathlib import Path

import torch
from huggingface_hub import snapshot_download
from safetensors import safe_open
from safetensors.torch import save_file

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import laya  # noqa: E402
from laya.agent import Agent  # noqa: E402
from laya.common import build_sequence, collate_items  # noqa: E402

from JFI.planner.judge import JudgeNode, build_state, questions_for  # noqa: E402

VERDICT_KEYS = {"GOOD": 0, "BREAKDOWN": 1, "REDO": 2}  # option order A, B, C
REASON_KEYS = {"operational": 0, "vague": 1, "duplicate": 2, "design": 3}
SUBFOLDER = {"english": None, "typed-decisions": "typed-decisions"}
REPO = "convaiinnovations/laya"


def load_rows(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def smoothed(k, n, eps):
    return [(1 - eps) + eps / n if i == k else eps / n for i in range(n)]


def items_for(row, agent, eps):
    node = JudgeNode(0, row["level"], row["node"], done_when=row.get("done_when", ""),
                     files=row.get("files", []), path=row.get("path", []))
    state = build_state(row["goal"], node)
    questions = questions_for(row["level"])
    max_len, head_max_len = agent.cfg.get("max_len", 512), agent.cfg.get("head_max_len", 192)
    out = []
    targets = [("verdict", VERDICT_KEYS[row["label"]])]
    if row["label"] == "REDO" and row.get("redo_reason") in REASON_KEYS:
        targets.append(("redo_reason", REASON_KEYS[row["redo_reason"]]))
    for qid, k in targets:
        q = Agent._to_internal(questions[qid])
        ids, markers = build_sequence(agent.tok, state, q, max_len, head_max_len)
        n = len(q["crit"])
        if len(markers) != n:
            raise ValueError(f"{qid}: {len(markers)} markers for {n} options -- sequence cut, check budgets")
        out.append({"ids": ids, "markers": markers, "qtype": 0, "target": smoothed(k, n, eps),
                    "label": k, "qid": qid, "row": row})
    return out


REDO_DONE_WHEN = {
    "operational": "the command runs and finishes without errors",
    "vague": "it is better than before",
}


def augment(rows):
    """Breaks two shortcuts in the hand-written data (observed on the first
    fine-tune: held-out GOOD/BREAKDOWN nodes with no files were judged REDO):
    every REDO row had empty `files` and `done_when`, while most GOOD /
    BREAKDOWN lead and task rows had both. Real nodes carry files and a
    done_when whatever their verdict, so the model must judge the text itself.
    Adds a files-less copy of every row that has files, and a copy of every
    REDO row with a (still operational / vague) done_when."""
    out = []
    for r in rows:
        out.append(r)
        if r.get("files"):
            out.append({**r, "files": []})
        if r["label"] == "REDO" and not r.get("done_when"):
            out.append({**r, "done_when": REDO_DONE_WHEN.get(r.get("redo_reason"), "it is done")})
    return out


def split(rows, val_frac, seed):
    """Holds out whole apps, not single rows, so a row and its paraphrases
    (data/paraphrase.py) always land on the same side -- a row-level split
    would put a rewrite of a validation row into training and inflate the
    score."""
    rng = random.Random(seed)

    def app(r):
        # app_id is set by paraphrase.py from the ORIGINAL goal, before goals
        # are rewritten row by row; older rows fall back to the goal itself.
        return r.get("app_id") or r["goal"]

    apps = sorted({app(r) for r in rows})
    rng.shuffle(apps)
    val_apps, n_val_rows = set(), 0
    for a in apps:
        if n_val_rows >= val_frac * len(rows):
            break
        val_apps.add(a)
        n_val_rows += sum(1 for r in rows if app(r) == a)
    return ([r for r in rows if app(r) not in val_apps],
            [r for r in rows if app(r) in val_apps])


class CachedEncoder(torch.nn.Module):
    """Stands in for the frozen encoder during training: returns each item's
    pre-computed last_hidden_state (padded to the batch), so every training
    step runs only the head -- through DecisionModel.forward itself, not a
    copy of it. The frozen encoder is ~95% of the compute and its output never
    changes, so recomputing it every epoch was pure waste (CPU: 73 s/epoch -> ~30 s)."""

    def __init__(self, real):
        super().__init__()
        self.config = real.config
        self.batch_hidden = None

    def forward(self, input_ids=None, attention_mask=None):
        return type("Out", (), {"last_hidden_state": self.batch_hidden})()


@torch.no_grad()
def cache_hidden(encoder, items, pad_id, batch):
    encoder.eval()
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        b = _to_device(collate_items([chunk], pad_id))
        h = encoder(input_ids=b["input_ids"], attention_mask=b["attention_mask"]).last_hidden_state
        for it, row in zip(chunk, h):
            it["hidden"] = row[: len(it["ids"])].float().clone()


DEVICE = torch.device("cpu")


def _to_device(b):
    return {k: (v.to(DEVICE) if isinstance(v, torch.Tensor) else v) for k, v in b.items()}


def forward(model, items, pad_id):
    b = _to_device(collate_items([items], pad_id))
    cached = isinstance(model.encoder, CachedEncoder)
    if cached:
        L = b["input_ids"].shape[1]
        d = items[0]["hidden"].shape[-1]
        h = torch.zeros(len(items), L, d, device=DEVICE)
        for i, it in enumerate(items):
            h[i, : it["hidden"].shape[0]] = it["hidden"]
        model.encoder.batch_hidden = h
    # bf16 autocast only when the encoder itself runs on the GPU (full fine-tune):
    # the RTX-class GPUs this targets support it, and it halves activation memory.
    with torch.autocast("cuda", dtype=torch.bfloat16, enabled=DEVICE.type == "cuda" and not cached):
        logits, _ = model(b["input_ids"], b["attention_mask"], b["marker_pos"], b["marker_mask"],
                          b["qtype"], detach_encoder=cached)
    return logits.float(), b


@torch.no_grad()
def evaluate(model, items, pad_id, batch):
    model.eval()
    correct, all_logits, all_labels = 0, [], []
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        logits, b = forward(model, chunk, pad_id)
        correct += (logits.argmax(-1) == b["label"]).sum().item()
        all_logits.append((logits.cpu(), b["marker_mask"].cpu()))
        all_labels.append(b["label"].cpu())
    return correct / max(1, len(items)), all_logits, all_labels


def fit_temperature(logit_batches, label_batches):
    """One temperature for choice questions, LBFGS on log T, clamped to [0.1, 10]
    (the notebook's recipe). Needs a held-out slice; returns 1.0 if it's tiny."""
    rows = sum(lb.numel() for lb in label_batches)
    if rows < 10:
        return 1.0
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=100)

    def closure():
        opt.zero_grad()
        loss = 0.0
        for (logits, mask), labels in zip(logit_batches, label_batches):
            scaled = (logits / log_t.exp()).masked_fill(~mask, -1e4)
            loss = loss + torch.nn.functional.cross_entropy(scaled, labels, reduction="sum")
        loss = loss / rows
        loss.backward()
        return loss

    try:
        opt.step(closure)
        return float(min(10.0, max(0.1, log_t.exp().item())))
    except Exception:
        return 1.2


def base_dir(base):
    sub = SUBFOLDER[base]
    prefix = f"{sub}/" if sub else ""
    root = snapshot_download(REPO, allow_patterns=[prefix + p for p in (
        "rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*")])
    return Path(root) / sub if sub else Path(root)


def save_checkpoint(model, src, out, temperature):
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in ("tokenizer", "encoder"):
        if (src / name).exists():
            shutil.copytree(src / name, out / name)
    cfg = json.loads((src / "rl_agent_config.json").read_text(encoding="utf-8"))
    temps = list(cfg.get("temperature", [1.0, 1.0, 1.0]))
    temps[0] = temperature
    cfg["temperature"] = temps
    # Inherited bucket temperatures take precedence at inference and would
    # silently mask the new fit (Laya docs/finetune.md).
    cfg.pop("temperature_by_options", None)
    cfg["jfi_finetune"] = {"base": str(src), "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    (out / "rl_agent_config.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")

    with safe_open(str(src / "model.safetensors"), "pt") as f:
        dtypes = {k: f.get_slice(k).get_dtype() for k in f.keys()}
    dtype_map = {"F16": torch.float16, "BF16": torch.bfloat16, "F32": torch.float32}
    state = {}
    for k, v in model.state_dict().items():
        v = v.detach().cpu().contiguous().clone()
        want = dtype_map.get(dtypes.get(k, ""))
        state[k] = v.to(want) if want is not None and v.is_floating_point() else v
    missing = set(dtypes) - set(state)
    if missing:
        raise RuntimeError(f"state dict is missing {len(missing)} base keys, e.g. {sorted(missing)[:3]}")
    save_file(state, str(out / "model.safetensors"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", choices=sorted(SUBFOLDER), default="english")
    ap.add_argument("--data", action="append", default=None,
                    help="training JSONL, repeatable (default: train.jsonl + train_para.jsonl if it exists)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--epochs", type=int, default=150)
    ap.add_argument("--lr", type=float, default=5e-4)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--smoothing", type=float, default=0.05)
    ap.add_argument("--patience", type=int, default=25)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default="auto", help="auto (cuda if available), cuda or cpu")
    ap.add_argument("--train-encoder", action="store_true",
                    help="full fine-tune: also train the encoder (needs a GPU in practice)")
    ap.add_argument("--encoder-lr", type=float, default=2.5e-5,
                    help="encoder learning rate with --train-encoder (Laya's notebook value)")
    args = ap.parse_args()
    global DEVICE
    DEVICE = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available()
                          else ("cpu" if args.device == "auto" else args.device))
    torch.manual_seed(args.seed)
    out = Path(args.out) if args.out else HERE / "checkpoints" / f"jfi-judge-{args.base}"

    t0 = time.perf_counter()
    agent = laya.load(REPO, device="cpu", subfolder=SUBFOLDER[args.base])
    model, pad_id = agent.model.float().to(DEVICE), agent.tok.pad_token_id
    print(f"loaded {args.base} in {time.perf_counter() - t0:.1f}s on {DEVICE}"
          + (f" ({torch.cuda.get_device_name(0)})" if DEVICE.type == "cuda" else ""))

    data_files = args.data or [str(p) for p in (HERE / "data" / "train.jsonl", HERE / "data" / "train_para.jsonl")
                               if p.exists()]
    rows = [r for path in data_files for r in load_rows(path)]
    print(f"data: {', '.join(Path(p).name for p in data_files)}")
    train_rows, val_rows = split(rows, args.val_frac, args.seed)
    train = [it for r in augment(train_rows) for it in items_for(r, agent, args.smoothing)]
    val = [it for r in val_rows for it in items_for(r, agent, 0.0)]
    val_verdict = [it for it in val if it["qid"] == "verdict"]
    print(f"rows: {len(train_rows)} train / {len(val_rows)} val -> items {len(train)} / {len(val)}")

    real_encoder = model.encoder
    if args.train_encoder:
        groups = [
            {"params": [p for p in model.encoder.parameters()], "lr": args.encoder_lr},
            {"params": [p for n, p in model.named_parameters() if not n.startswith("encoder.")], "lr": args.lr},
        ]
        mode = "full fine-tune"
    else:
        for p in model.encoder.parameters():
            p.requires_grad = False
        t1 = time.perf_counter()
        cache_hidden(model.encoder, train + val, pad_id, args.batch)
        model.encoder = CachedEncoder(real_encoder)
        print(f"cached encoder outputs for {len(train) + len(val)} items in {time.perf_counter() - t1:.0f}s")
        groups = [{"params": [p for p in model.parameters() if p.requires_grad], "lr": args.lr}]
        mode = "encoder frozen"
    trainable = [p for g in groups for p in g["params"]]
    print(f"trainable params: {sum(p.numel() for p in trainable) / 1e6:.1f}M ({mode})")
    opt = torch.optim.AdamW(groups, weight_decay=0.01)
    total_steps = args.epochs * ((len(train) + args.batch - 1) // args.batch)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=[g["lr"] for g in groups],
                                                total_steps=total_steps, pct_start=0.1)

    base_acc, _, _ = evaluate(model, val_verdict, pad_id, args.batch)
    print(f"epoch 0: val verdict acc {base_acc:.0%} (base checkpoint)")
    best_acc, best_state, stale = base_acc, None, 0
    rng = random.Random(args.seed)
    for epoch in range(1, args.epochs + 1):
        model.train()
        if not args.train_encoder:
            model.encoder.eval()  # frozen: no dropout noise from it
        rng.shuffle(train)
        running = 0.0
        for i in range(0, len(train), args.batch):
            chunk = train[i:i + args.batch]
            logits, b = forward(model, chunk, pad_id)
            logp = torch.log_softmax(logits, -1)
            loss = -(b["target"] * logp).sum(-1).mean()
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(trainable, 1.0)
            opt.step()
            sched.step()
            running += loss.item() * len(chunk)
        acc, _, _ = evaluate(model, val_verdict, pad_id, args.batch)
        if epoch % 5 == 0 or acc > best_acc:
            print(f"epoch {epoch}: train loss {running / len(train):.3f}  val verdict acc {acc:.0%}  "
                  f"({time.perf_counter() - t0:.0f}s)")
        if acc > best_acc or best_state is None:
            best_acc, stale = acc, 0
            best_state = {n: p.detach().clone() for n, p in model.named_parameters() if p.requires_grad}
            if args.train_encoder:
                best_state.update({f"encoder.{n}": p.detach().clone()
                                   for n, p in real_encoder.named_parameters()})
        else:
            stale += 1
            if stale >= args.patience:
                print("early stop")
                break

    with torch.no_grad():
        params = dict(model.named_parameters())
        for n, v in best_state.items():
            if n in params:
                params[n].copy_(v)
    _, logits, labels = evaluate(model, val, pad_id, args.batch)
    temperature = fit_temperature(logits, labels)
    model.encoder = real_encoder
    print(f"best val verdict acc {best_acc:.0%}; fitted choice temperature {temperature:.2f}")
    save_checkpoint(model, base_dir(args.base), out, temperature)
    print(f"saved {out} ({time.perf_counter() - t0:.0f}s total)")


if __name__ == "__main__":
    main()
