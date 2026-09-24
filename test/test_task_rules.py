"""
Tests for task_rules.detect_task_type -- a cheap, deterministic keyword
classifier over the goal text, used by AdaptiveSessionManager to pick
task-type-specific planning rules. Real benchmark task prompts (see
benchmark/tasks/) are used as regression fixtures where practical, since
those are exactly the goal shapes this needs to classify correctly.
"""
from JFI.session.task_rules import CORE_PLAN_RULES, TYPE_ADDENDA, detect_task_type


def test_python_goal():
    goal = (
        "Build a command-line calculator application in Python. The REPL entry "
        "point MUST be a file named main.py, runnable as `python3 main.py`."
    )
    assert detect_task_type(goal) == "python"


def test_javascript_goal():
    goal = "Build a portfolio dashboard as a Vite + vanilla JavaScript Node.js project."
    assert detect_task_type(goal) == "javascript"


def test_html_only_goal_classified_as_html_css():
    goal = "Write a static pricing table page in HTML and CSS with three tiers."
    assert detect_task_type(goal) == "html_css"


def test_html_css_plus_framework_stays_javascript():
    # A framework/build-tool mention means it's a real app, not a static
    # page, even if HTML/CSS are also named.
    goal = "Build a Vite app with HTML and CSS styling for a pricing page."
    assert detect_task_type(goal) == "javascript"


def test_sql_goal():
    goal = "Design a Postgres schema for users and orders, then write a SQL query for top spenders."
    assert detect_task_type(goal) == "sql"


def test_sqlite_hint():
    goal = "Create a sqlite database with a CREATE TABLE statement for a todo list."
    assert detect_task_type(goal) == "sql"


def test_story_goal():
    goal = "Write a short story about a lighthouse keeper who finds a mysterious archive."
    assert detect_task_type(goal) == "story"


def test_unrelated_goal_is_generic():
    goal = "Summarize the attached PDF into three bullet points."
    assert detect_task_type(goal) == "generic"


def test_empty_goal_is_generic():
    assert detect_task_type("") == "generic"
    assert detect_task_type(None) == "generic"


def test_mixed_python_and_javascript_prefers_javascript():
    # A visual-surface (JS/frontend) goal carries more architecture risk
    # than an incidental backend-language mention -- see task_rules'
    # module docstring for why javascript wins this particular tie.
    goal = "Build a Flask backend that serves data to a React frontend."
    assert detect_task_type(goal) == "javascript"


def test_story_mention_of_python_word_does_not_flip_to_python():
    goal = "Write a short story about a programmer named Python who debugs dreams."
    assert detect_task_type(goal) == "story"


def test_data_engineering_goal():
    goal = "Clean sales.csv with pandas, drop nulls, and aggregate monthly revenue by region into a DataFrame."
    assert detect_task_type(goal) == "data_engineering"


def test_data_engineering_etl_wording():
    goal = "Build an ETL data pipeline that loads a parquet file, transforms it, and writes a summary."
    assert detect_task_type(goal) == "data_engineering"


def test_plain_csv_mention_is_not_data_engineering():
    # A bare "csv" mention is common in unrelated tasks (see the StockUI
    # data-service goal, which fetched CSVs from a javascript frontend) --
    # only pandas/ETL-specific vocabulary should trigger this type.
    goal = "Fetch sectors.csv from the server and render it in a table."
    assert detect_task_type(goal) == "generic"


def test_csv_plus_javascript_goal_stays_javascript():
    goal = "Build a Vite dashboard that fetches transactions.csv and renders it in a React table."
    assert detect_task_type(goal) == "javascript"


def test_go_goal():
    goal = "Write a Go CLI tool with a go.mod file that fetches weather data using goroutines."
    assert detect_task_type(goal) == "go"


def test_go_file_extension_hint():
    goal = "Add error handling to main.go and write table-driven tests, then run go test ./..."
    assert detect_task_type(goal) == "go"


def test_pandas_goal_no_longer_matches_generic_python():
    # pandas moved from the generic python hints to data_engineering's own
    # hints -- a pandas-only goal should classify as data_engineering, not
    # python, even with no other python-specific wording.
    goal = "Use pandas to summarize the numbers in report.csv."
    assert detect_task_type(goal) == "data_engineering"


def test_every_known_type_has_an_addendum_and_formats_cleanly():
    for task_type, addendum in TYPE_ADDENDA.items():
        combined = CORE_PLAN_RULES.format(plan_path="JFI/demo/plan.md", session_dir="JFI/demo") + addendum
        assert "JFI/demo" in combined
        assert "get_leaf" in combined
        # Never literally "plan.md" anywhere, not even in a "there is no
        # plan.md" negation -- the word alone used to be enough to send the
        # model off checking for a nonexistent file (it doesn't exist for a
        # DB-backed session; see session_dir's own docstring above).
        assert "plan.md" not in combined
        assert combined.strip()


def test_generic_has_no_addendum():
    assert "generic" not in TYPE_ADDENDA
