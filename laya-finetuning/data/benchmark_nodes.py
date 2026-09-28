"""Labelled plan nodes derived from JFI's own benchmark goals (benchmark/tasks/).

Split by PROBLEM, never by row: a problem's Python and JS versions are near-
identical goals, so both land on the same side, or the test set would leak.
Story tasks are left out: they take the document path (laya_plan.md G4),
which the code-path judge questions don't cover.

Run to (re)generate the benchmark rows in train.jsonl / test.jsonl:
    python laya-finetuning/data/benchmark_nodes.py
Row tuple: (level, path, node, done_when, files, label, redo_reason)
"""
import json
from pathlib import Path

B, G, R = "BREAKDOWN", "GOOD", "REDO"
HERE = Path(__file__).resolve().parent
TASKS = HERE.parents[1] / "benchmark" / "tasks"

TRAIN_TASKS = ["difference_of_squares", "difference_of_squares_js", "run_length_encoding",
               "run_length_encoding_js", "spiral_matrix", "spiral_matrix_js", "sales_summary",
               "recipe_card", "pricing_table", "react_counter"]
TEST_TASKS = ["anagram_groups", "anagram_groups_js", "collatz_conjecture", "collatz_conjecture_js",
              "log_pipeline", "faq_accordion", "nextjs_static", "calc"]

NODES = {
    # ------------------------------------------------------------ train
    "difference_of_squares": [
        ("architect", [], "design the difference_of_squares module in difference_of_squares.py: square_of_sum and sum_of_squares", "both functions return the right values for n=10", [], B, None),
        ("architect", [], "write README.md explaining square_of_sum and sum_of_squares", "README.md describes both functions", ["README.md"], G, None),
        ("lead", ["difference_of_squares module"], "scaffold difference_of_squares.py: square_of_sum(n) and sum_of_squares(n)", "both stubs implemented and tested", ["difference_of_squares.py", "tests/test_mine.py"], B, None),
        ("task", ["difference_of_squares module", "difference_of_squares.py"], "implement square_of_sum(n: int) -> int in difference_of_squares.py", "square_of_sum(10) == 3025", ["difference_of_squares.py", "tests/test_mine.py"], G, None),
        ("task", ["difference_of_squares module", "difference_of_squares.py"], "implement sum_of_squares(n: int) -> int in difference_of_squares.py", "sum_of_squares(10) == 385", ["difference_of_squares.py", "tests/test_mine.py"], G, None),
        ("task", ["difference_of_squares module", "tests"], "run pytest tests -q and make sure every test passes", "all tests pass", ["tests/test_difference_of_squares.py"], R, "operational"),
        ("lead", ["difference_of_squares module"], "make the maths correct", "", [], R, "vague"),
    ],
    "difference_of_squares_js": [
        ("architect", [], "design the difference_of_squares.js CommonJS module exporting squareOfSum and sumOfSquares", "both exports return the right values for n=10", [], B, None),
        ("lead", ["difference_of_squares.js module"], "scaffold difference_of_squares.js: squareOfSum(n), sumOfSquares(n) and the module.exports line", "all stubs implemented and tested", ["difference_of_squares.js", "tests/mine.test.js"], B, None),
        ("task", ["difference_of_squares.js module", "difference_of_squares.js"], "implement squareOfSum(n) in difference_of_squares.js", "squareOfSum(10) === 3025", ["difference_of_squares.js", "tests/mine.test.js"], G, None),
        ("task", ["difference_of_squares.js module", "difference_of_squares.js"], "integrate squareOfSum and sumOfSquares into module.exports in difference_of_squares.js", "require('./difference_of_squares') exposes both", ["difference_of_squares.js", "tests/mine.test.js"], G, None),
        ("architect", [], "run node --test tests/*.test.js from the project root", "the command exits 0", [], R, "operational"),
    ],
    "run_length_encoding": [
        ("architect", [], "design the run_length_encoding module in run_length_encoding.py: encode and decode", "decode(encode(x)) == x", [], B, None),
        ("lead", ["run_length_encoding module"], "scaffold run_length_encoding.py: encode(s) and decode(s)", "both stubs implemented and tested", ["run_length_encoding.py", "tests/test_mine.py"], B, None),
        ("task", ["run_length_encoding module", "run_length_encoding.py"], "implement encode(s: str) -> str in run_length_encoding.py: runs of 2+ become '<count><char>'", "encode('aaabccc') == '3abc3c'", ["run_length_encoding.py", "tests/test_mine.py"], G, None),
        ("task", ["run_length_encoding module", "run_length_encoding.py"], "implement decode(s: str) -> str in run_length_encoding.py expanding '<count><char>' pairs", "decode('3abc3c') == 'aaabccc'", ["run_length_encoding.py", "tests/test_mine.py"], G, None),
        ("lead", ["project"], "scaffold README.md explaining the encoding scheme", "README.md explains runs and single characters", ["README.md"], G, None),
        ("lead", ["run_length_encoding module"], "run pytest tests -q until it passes", "", ["tests/test_run_length_encoding.py"], R, "operational"),
        ("task", ["run_length_encoding module", "run_length_encoding.py"], "handle the edge cases somehow", "", [], R, "vague"),
    ],
    "run_length_encoding_js": [
        ("architect", [], "design the run_length_encoding.js CommonJS module exporting encode and decode", "decode(encode(x)) === x", [], B, None),
        ("lead", ["run_length_encoding.js module"], "scaffold run_length_encoding.js: encode(s), decode(s) and module.exports", "all stubs implemented and tested", ["run_length_encoding.js", "tests/mine.test.js"], B, None),
        ("task", ["run_length_encoding.js module", "run_length_encoding.js"], "implement decode(s) in run_length_encoding.js", "decode('3abc3c') === 'aaabccc'", ["run_length_encoding.js", "tests/mine.test.js"], G, None),
        ("task", ["run_length_encoding.js module", "run_length_encoding.js"], "implement the whole module in run_length_encoding.js: encode, decode, exports, input checks, a CLI and docs", "it works", ["run_length_encoding.js"], B, None),
        ("task", ["run_length_encoding.js module", "tests"], "run node --test tests/*.test.js and check the output", "", [], R, "operational"),
    ],
    "spiral_matrix": [
        ("architect", [], "design the spiral_matrix module in spiral_matrix.py: spiral(n) building a clockwise n x n matrix", "spiral(3) matches the expected matrix", [], B, None),
        ("lead", ["spiral_matrix module"], "scaffold spiral_matrix.py with the single spiral(n) function", "spiral is implemented and tested", ["spiral_matrix.py", "tests/test_mine.py"], G, None),
        ("task", ["spiral_matrix module", "spiral_matrix.py"], "implement spiral(n: int) -> list[list[int]] in spiral_matrix.py raising ValueError for n < 0", "spiral(2) == [[1, 2], [4, 3]]", ["spiral_matrix.py", "tests/test_mine.py"], G, None),
        ("architect", [], "write README.md explaining the spiral rule with a small example", "README.md shows spiral(3)", ["README.md"], G, None),
        ("task", ["spiral_matrix module", "tests"], "run pytest tests -q and fix whatever fails", "every test passes", ["tests/test_spiral_matrix.py"], R, "operational"),
        ("lead", ["spiral_matrix module"], "get the spiral right", "", [], R, "vague"),
    ],
    "spiral_matrix_js": [
        ("architect", [], "design the spiral_matrix.js CommonJS module exporting spiral(n)", "spiral(3) matches the expected matrix", [], B, None),
        ("lead", ["spiral_matrix.js module"], "scaffold spiral_matrix.js: spiral(n) and the module.exports line", "all stubs implemented and tested", ["spiral_matrix.js", "tests/mine.test.js"], B, None),
        ("task", ["spiral_matrix.js module", "spiral_matrix.js"], "implement spiral(n) in spiral_matrix.js throwing an Error for n < 0", "spiral(0) returns []", ["spiral_matrix.js", "tests/mine.test.js"], G, None),
        ("lead", ["spiral_matrix.js module"], "node --test tests/ to check the spiral", "", [], R, "operational"),
    ],
    "sales_summary": [
        ("architect", [], "design the sales summary module in sales_summary.py: summarize(csv_path) with pandas", "summarize('tests/sales_data.csv') returns the three fields", [], B, None),
        ("architect", [], "declare pandas as the single dependency in pyproject.toml", "pyproject.toml lists pandas", ["pyproject.toml"], G, None),
        ("lead", ["sales summary module"], "scaffold sales_summary.py: load_sales, revenue_by_region, top_product_by_quantity and summarize", "all stubs implemented and tested", ["sales_summary.py", "tests/test_mine.py"], B, None),
        ("task", ["sales summary module", "sales_summary.py"], "implement revenue_by_region(df) -> dict in sales_summary.py rounding to 2 decimals", "two North rows of 10.0 and 5.5 give {'North': 15.5}", ["sales_summary.py", "tests/test_mine.py"], G, None),
        ("task", ["sales summary module", "sales_summary.py"], "implement top_product_by_quantity(df) -> str in sales_summary.py breaking ties alphabetically", "a 5/5 tie between 'b' and 'a' returns 'a'", ["sales_summary.py", "tests/test_mine.py"], G, None),
        ("task", ["sales summary module", "sales_summary.py"], "implement summarize(csv_path) in sales_summary.py: read the CSV, clean it, compute revenue, regions, top product, round and validate everything", "summarize works", ["sales_summary.py"], B, None),
        ("architect", [], "pip install pandas and run pytest tests -q", "", [], R, "operational"),
        ("task", ["sales summary module", "sales_summary.py"], "make the numbers right", "the output is correct", [], R, "vague"),
    ],
    "recipe_card": [
        ("architect", [], "design the recipe page in index.html and style.css: title, ingredients, steps and an image", "the page shows every required element", [], B, None),
        ("lead", ["recipe page"], "scaffold index.html: the data-recipe-title heading, the data-ingredient list, the data-step list and the <img>", "all sections present", ["index.html"], B, None),
        ("task", ["recipe page", "index.html"], "implement the ingredient list in index.html: at least 5 <li data-ingredient> items", "5 elements carry data-ingredient", ["index.html"], G, None),
        ("task", ["recipe page", "style.css"], "implement the card layout rules in style.css: width, padding, shadow and fonts", "the card is centred with a shadow", ["style.css"], G, None),
        ("task", ["recipe page", "index.html"], "build the whole recipe page in index.html: title, ingredients, steps, image, styling and responsive layout", "the page is done", ["index.html"], B, None),
        ("lead", ["recipe page"], "open index.html directly in a browser to check it looks right", "it looks right", ["index.html"], R, "operational"),
        ("architect", [], "make it look nice", "", [], R, "vague"),
    ],
    "pricing_table": [
        ("architect", [], "design the pricing page in index.html and style.css: three data-plan tiers side by side", "three tiers render side by side", [], B, None),
        ("lead", ["pricing page"], "scaffold index.html: three <section data-plan> tiers, each with data-price, features and a data-cta button", "all three tiers present", ["index.html"], B, None),
        ("task", ["pricing page", "index.html"], "implement the Pro tier in index.html: <section data-plan='Pro'> with data-price, 3 <li> features and a data-cta button", "the Pro section has all four parts", ["index.html"], G, None),
        ("task", ["pricing page", "style.css"], "implement the three-column flex layout for the tiers in style.css", "the tiers sit side by side", ["style.css"], G, None),
        ("lead", ["project"], "scaffold README.md explaining how to open the page", "README.md says to open index.html", ["README.md"], G, None),
        ("task", ["pricing page", "index.html"], "view the page in the browser", "", [], R, "operational"),
        ("lead", ["pricing page"], "improve the styling", "", [], R, "vague"),
    ],
    "react_counter": [
        ("architect", [], "design the counter page in index.html: React 18 from a CDN and a Counter component without JSX", "the counter renders and updates", [], B, None),
        ("lead", ["counter page"], "scaffold index.html: the CDN <script> tags, the mount point and the Counter component", "all parts present", ["index.html"], B, None),
        ("task", ["counter page", "index.html"], "implement the Counter component with React.createElement and useState in index.html: count, increment and reset", "clicking increment twice shows 2", ["index.html"], G, None),
        ("task", ["counter page", "index.html"], "integrate Counter into the mount point with ReactDOM.createRoot in index.html", "the counter renders at 0", ["index.html"], G, None),
        ("task", ["counter page", "index.html"], "open index.html and click the buttons to see if the count updates", "the count updates", ["index.html"], R, "operational"),
        ("architect", [], "make the counter work", "", [], R, "vague"),
    ],
    # ------------------------------------------------------------ test
    "anagram_groups": [
        ("architect", [], "design the anagram_groups module in anagram_groups.py: group_anagrams(words)", "the grouping and ordering rules hold", [], B, None),
        ("lead", ["anagram_groups module"], "scaffold anagram_groups.py: _key(word), group_anagrams(words) and _sort_groups(groups)", "all stubs implemented and tested", ["anagram_groups.py", "tests/test_mine.py"], B, None),
        ("task", ["anagram_groups module", "anagram_groups.py"], "implement _key(word: str) -> str in anagram_groups.py: the lowercase sorted letters", "_key('Listen') == _key('Silent')", ["anagram_groups.py", "tests/test_mine.py"], G, None),
        ("task", ["anagram_groups module", "anagram_groups.py"], "implement _sort_groups(groups) in anagram_groups.py: sort each group, then the groups by first word", "[['b', 'a'], ['c']] becomes [['a', 'b'], ['c']]", ["anagram_groups.py", "tests/test_mine.py"], G, None),
        ("architect", [], "write README.md explaining the grouping and ordering rules", "README.md explains both rules", ["README.md"], G, None),
        ("task", ["anagram_groups module", "tests"], "run pytest tests -q and keep going until it is green", "", ["tests/test_anagram_groups.py"], R, "operational"),
        ("lead", ["anagram_groups module"], "deal with the anagram logic", "", [], R, "vague"),
    ],
    "anagram_groups_js": [
        ("architect", [], "design the anagram_groups.js CommonJS module exporting groupAnagrams", "groupAnagrams follows the ordering rules", [], B, None),
        ("lead", ["anagram_groups.js module"], "scaffold anagram_groups.js: key(word), groupAnagrams(words), sortGroups(groups) and module.exports", "all stubs implemented and tested", ["anagram_groups.js", "tests/mine.test.js"], B, None),
        ("task", ["anagram_groups.js module", "anagram_groups.js"], "implement groupAnagrams(words) in anagram_groups.js returning [] for an empty input", "groupAnagrams([]) deep-equals []", ["anagram_groups.js", "tests/mine.test.js"], G, None),
        ("task", ["anagram_groups.js module", "anagram_groups.js"], "implement the entire anagram feature in anagram_groups.js: normalising, grouping, both sorts, duplicates, exports, a CLI and README", "it works", ["anagram_groups.js"], B, None),
        ("lead", ["anagram_groups.js module"], "run node --test tests/*.test.js from the project root", "all tests pass", ["tests/test_anagram_groups.test.js"], R, "operational"),
    ],
    "collatz_conjecture": [
        ("architect", [], "design the collatz module in collatz_conjecture.py: steps(n)", "steps(1) == 0 and steps(16) == 4", [], B, None),
        ("lead", ["collatz module"], "scaffold collatz_conjecture.py with the single steps(n) function", "steps is implemented and tested", ["collatz_conjecture.py", "tests/test_mine.py"], G, None),
        ("task", ["collatz module", "collatz_conjecture.py"], "implement steps(n: int) -> int in collatz_conjecture.py raising ValueError for n < 1", "steps(16) == 4", ["collatz_conjecture.py", "tests/test_mine.py"], G, None),
        ("task", ["collatz module", "tests"], "run pytest tests -q", "", [], R, "operational"),
        ("architect", [], "make collatz better", "", [], R, "vague"),
    ],
    "collatz_conjecture_js": [
        ("architect", [], "design the collatz_conjecture.js CommonJS module exporting steps(n)", "steps(16) === 4", [], B, None),
        ("lead", ["collatz_conjecture.js module"], "scaffold collatz_conjecture.js: steps(n) and the module.exports line", "all stubs implemented and tested", ["collatz_conjecture.js", "tests/mine.test.js"], B, None),
        ("task", ["collatz_conjecture.js module", "collatz_conjecture.js"], "implement steps(n) in collatz_conjecture.js throwing an Error for n < 1", "steps(1) === 0", ["collatz_conjecture.js", "tests/mine.test.js"], G, None),
        ("task", ["collatz_conjecture.js module", "tests"], "execute node --test tests/*.test.js", "", ["tests/test_collatz_conjecture.test.js"], R, "operational"),
    ],
    "log_pipeline": [
        ("architect", [], "design the log pipeline module in log_pipeline.py: parse_logs(path) with the four summary fields", "parse_logs('tests/access.log') returns the four fields", [], B, None),
        ("lead", ["log pipeline module"], "scaffold log_pipeline.py: parse_line, count_errors_by_service, busiest_hour and parse_logs", "all stubs implemented and tested", ["log_pipeline.py", "tests/test_mine.py"], B, None),
        ("task", ["log pipeline module", "log_pipeline.py"], "implement parse_line(line: str) -> tuple in log_pipeline.py splitting timestamp, level, service and message", "the sample ERROR auth line parses into four parts", ["log_pipeline.py", "tests/test_mine.py"], G, None),
        ("task", ["log pipeline module", "log_pipeline.py"], "implement busiest_hour(hours: list[str]) -> str in log_pipeline.py breaking ties by the smallest hour", "['05', '03', '05', '03'] gives '03'", ["log_pipeline.py", "tests/test_mine.py"], G, None),
        ("task", ["log pipeline module", "log_pipeline.py"], "implement parse_logs(path) in log_pipeline.py: read the file, parse every line, count levels, errors per service, error rate, hours, rounding and validation", "parse_logs works", ["log_pipeline.py"], B, None),
        ("architect", [], "write README.md explaining the four output fields", "README.md explains each field", ["README.md"], G, None),
        ("lead", ["log pipeline module"], "run pytest tests -q on the fixed dataset", "", [], R, "operational"),
        ("task", ["log pipeline module", "log_pipeline.py"], "improve the parsing", "", [], R, "vague"),
    ],
    "faq_accordion": [
        ("architect", [], "design the FAQ page in index.html and style.css: five <details data-faq> items", "five accordion items open and close", [], B, None),
        ("lead", ["FAQ page"], "scaffold index.html: the page heading and five <details data-faq> items with <summary> questions", "all five items present", ["index.html"], B, None),
        ("task", ["FAQ page", "index.html"], "implement the shipping question in index.html: one <details data-faq> with a <summary> and a real answer", "the item opens to show its answer", ["index.html"], G, None),
        ("task", ["FAQ page", "style.css"], "implement the <summary> marker style in style.css replacing the default triangle with a plus icon", "the default marker is hidden", ["style.css"], G, None),
        ("lead", ["project"], "scaffold README.md explaining how to open the page", "README.md says to open index.html", ["README.md"], G, None),
        ("task", ["FAQ page", "index.html"], "open the page and click every question", "", ["index.html"], R, "operational"),
        ("architect", [], "polish the FAQ", "", [], R, "vague"),
    ],
    "nextjs_static": [
        ("architect", [], "design the site pages in pages/: the home page and the about page", "both pages build and export", [], B, None),
        ("architect", [], "set output: 'export' in next.config.mjs", "next.config.mjs has output: 'export'", ["next.config.mjs"], G, None),
        ("lead", ["site pages"], "scaffold pages/index.js: the Home component with data-page='home' and the <Link> to /about", "all stubs implemented and tested", ["pages/index.js", "tests/home.test.js"], B, None),
        ("task", ["site pages", "pages/about.js"], "implement the About component in pages/about.js with data-page='about' and real text", "the about page renders its text", ["pages/about.js", "tests/about.test.js"], G, None),
        ("task", ["site pages", "pages/index.js"], "integrate the next/link <Link> to /about with data-testid='nav-about' in pages/index.js", "the home page links to /about", ["pages/index.js", "tests/home.test.js"], G, None),
        ("architect", [], "scaffold the project with npx create-next-app", "", [], R, "operational"),
        ("task", ["site pages", "pages/index.js"], "run npm run build and fix the errors", "the build passes", [], R, "operational"),
        ("lead", ["site pages"], "tidy up the pages", "", [], R, "vague"),
    ],
    "calc": [
        ("architect", [], "design the calculator core in calc/core.py: parse and evaluate '<number> <op> <number>' lines", "'2 + 2' evaluates to 4", [], B, None),
        ("architect", [], "design the REPL entry point in main.py: read stdin lines until exit or quit", "exit ends the loop with status 0", [], B, None),
        ("lead", ["calculator core"], "scaffold calc/core.py: parse_line, evaluate and format_result", "all stubs implemented and tested", ["calc/core.py", "tests/test_core.py"], B, None),
        ("task", ["calculator core", "calc/core.py"], "implement evaluate(a: float, op: str, b: float) -> float in calc/core.py supporting + - * / ^", "evaluate(2, '^', 3) == 8", ["calc/core.py", "tests/test_core.py"], G, None),
        ("task", ["REPL", "main.py"], "implement main() in main.py: read input, parse it, evaluate, print, handle division by zero, bad input and exit, and add tests", "the calculator works", ["main.py"], B, None),
        ("task", ["REPL", "main.py"], "integrate the division-by-zero error message into the REPL loop in main.py", "'1 / 0' prints an error and continues", ["main.py", "tests/test_main.py"], G, None),
        ("lead", ["REPL"], "run python3 main.py and type some sums", "", ["main.py"], R, "operational"),
        ("task", ["calculator core", "calc/core.py"], "make the calculator smarter", "", [], R, "vague"),
    ],
}


def goal(task_id):
    return json.loads((TASKS / "polyglot" / task_id / "task.json").read_text(encoding="utf-8"))["prompt"] \
        if (TASKS / "polyglot" / task_id).exists() else next(
            json.loads(p.read_text(encoding="utf-8"))["prompt"] for p in TASKS.glob(f"*/{task_id}/task.json"))


def rows(task_ids):
    for tid in task_ids:
        g = goal(tid)
        for level, path, node, done_when, files, label, reason in NODES[tid]:
            yield {"goal": g, "level": level, "path": path, "node": node, "done_when": done_when,
                   "files": files, "label": label, "redo_reason": reason, "source": f"benchmark:{tid}"}


def write(path, new_rows):
    """Replaces this generator's rows in `path`, keeping every other row."""
    existing = []
    if path.exists():
        existing = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    kept = [r for r in existing if not str(r.get("source", "")).startswith("benchmark:")]
    with path.open("w", encoding="utf-8") as f:
        for r in kept + list(new_rows):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(kept)


if __name__ == "__main__":
    assert not set(TRAIN_TASKS) & set(TEST_TASKS)
    assert set(TRAIN_TASKS) | set(TEST_TASKS) == set(NODES)
    from collections import Counter
    for name, ids in (("train", TRAIN_TASKS), ("test", TEST_TASKS)):
        new = list(rows(ids))
        kept = write(HERE / f"{name}.jsonl", new)
        total = kept + len(new)
        print(f"{name}.jsonl: {kept} existing + {len(new)} benchmark = {total} | "
              f"{dict(Counter(r['label'] for r in new))}")
