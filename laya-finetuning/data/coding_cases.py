"""100 rows of coding work the earlier data barely covered: changes to EXISTING
code (modify, bug fix, delete, refactor -- laya_plan.md G3), non-function
files (config, CI, Dockerfile, migration, docs, dependency bumps -- G2),
tests-only work, security and performance fixes, investigation, and new
operational look-alikes (git, deploy, rollback).

Near-miss contrasts on purpose: writing a Dockerfile or a CI workflow is
build work (GOOD); running `docker build` or the test suite is operational
(REDO).

Split BY APP: 7 apps (70 rows) go to train.jsonl, 3 other apps (30 rows) to
test.jsonl, so the held-out set measures these case types too. Each row
carries a `case` tag for per-type accuracy.

    python laya-finetuning/data/coding_cases.py   # (re)writes its rows in both files
Row tuple: (case, level, path, node, done_when, files, label, redo_reason)
"""
import json
from pathlib import Path

B, G, R = "BREAKDOWN", "GOOD", "REDO"
HERE = Path(__file__).resolve().parent

TRAIN_APPS = {
    "An existing FastAPI todo service (app/, SQLModel, pytest): add due dates and overdue sorting.": [
        ("modify", "task", ["api", "app/api/todos.py"], "change list_todos() in app/api/todos.py to return overdue todos first", "an overdue todo is listed before a newer one", ["app/api/todos.py", "tests/test_todos.py"], G, None),
        ("modify", "lead", ["persistence"], "mark app/db/models.py: add a due_date field to the existing Todo model", "Todo has an optional due_date", ["app/db/models.py", "tests/test_models.py"], G, None),
        ("migration", "task", ["persistence", "migrations"], "write the alembic migration adding the due_date column to the todo table", "alembic upgrade head adds the column", ["migrations/versions/0002_due_date.py"], G, None),
        ("delete", "task", ["api", "app/api/todos.py"], "delete the now-unused sort_by_created() from app/api/todos.py", "nothing references sort_by_created", ["app/api/todos.py"], G, None),
        ("test_only", "task", ["api", "tests/test_todos.py"], "add a unit test for is_overdue() covering a todo due today", "is_overdue(today) is False", ["tests/test_todos.py"], G, None),
        ("refactor", "architect", [], "refactor the whole persistence layer from sync SQLModel sessions to async SQLAlchemy", "every endpoint uses async sessions", [], B, None),
        ("git_op", "architect", [], "commit the changes and push to main", "", [], R, "operational"),
        ("git_op", "task", ["api", "app/api/todos.py"], "open a pull request for the due date feature", "", [], R, "operational"),
        ("vague", "lead", ["api"], "add proper error handling everywhere", "", [], R, "vague"),
        ("bugfix", "task", ["api", "app/api/todos.py"], "fix search_todos() in app/api/todos.py returning 500 on an empty query", "search_todos('') returns []", ["app/api/todos.py", "tests/test_todos.py"], G, None),
    ],
    "An existing Express + TypeScript e-commerce API (src/, Jest): harden it for production.": [
        ("security", "task", ["auth", "src/auth/passwords.ts"], "change hashPassword() in src/auth/passwords.ts to use bcrypt with 12 rounds", "the hash verifies with bcrypt.compare", ["src/auth/passwords.ts", "test/passwords.test.ts"], G, None),
        ("security", "task", ["orders", "src/orders/repo.ts"], "fix the SQL injection in findOrdersByEmail() in src/orders/repo.ts by using a parameterised query", "an email containing a quote returns no rows and no error", ["src/orders/repo.ts", "test/repo.test.ts"], G, None),
        ("config", "lead", ["project"], "write .env.example listing DATABASE_URL, JWT_SECRET and STRIPE_KEY", ".env.example lists all three keys", [".env.example"], G, None),
        ("docker", "lead", ["project"], "write a Dockerfile: node:20-alpine, install production deps, copy dist, run node dist/index.js", "docker build succeeds with this Dockerfile", ["Dockerfile"], G, None),
        ("docker", "architect", [], "docker build -t shop . and docker run -p 3000:3000 shop", "", [], R, "operational"),
        ("ci", "lead", ["project"], "write .github/workflows/ci.yml running npm ci, npm run build and npm test on every push", "the workflow file lists the three steps", [".github/workflows/ci.yml"], G, None),
        ("performance", "task", ["orders", "src/orders/service.ts"], "fix the N+1 query in listOrders() in src/orders/service.ts by loading items with one join", "listing 10 orders runs 1 query", ["src/orders/service.ts", "test/service.test.ts"], G, None),
        ("refactor", "task", ["orders", "src/orders/service.ts"], "rewrite src/orders/service.ts: split it into repository, pricing, tax, shipping, payment and notification modules with tests", "the service is split", ["src/orders/service.ts"], B, None),
        ("deploy_op", "task", ["project", "Dockerfile"], "deploy the new image to production and watch the logs", "", [], R, "operational"),
        ("vague", "architect", [], "make it production ready", "", [], R, "vague"),
    ],
    "An existing Django blog (blog/, pytest-django): add tags and tidy the codebase.": [
        ("new_feature", "architect", [], "design the tags component in blog/tags/: tag model, tagging posts and a tag page", "a post can be tagged and listed by tag", [], B, None),
        ("new_feature", "lead", ["tags"], "scaffold blog/tags/services.py: tag_post, untag_post, posts_for_tag and popular_tags", "all stubs implemented and tested", ["blog/tags/services.py", "tests/test_tags.py"], B, None),
        ("migration", "lead", ["tags"], "write the Django migration creating the Tag model and the post-tag table", "migrate creates both tables", ["blog/tags/migrations/0001_initial.py"], G, None),
        ("modify", "task", ["posts", "blog/posts/views.py"], "change post_detail() in blog/posts/views.py to include the post's tags in the context", "the context has a tags list", ["blog/posts/views.py", "tests/test_views.py"], G, None),
        ("delete", "lead", ["posts"], "remove the deprecated blog/posts/legacy_feed.py module and its URL route", "no route points at legacy_feed", ["blog/posts/legacy_feed.py", "blog/urls.py"], G, None),
        ("docs", "architect", [], "update README.md with the new tagging feature and how to use it", "README.md has a Tags section", ["README.md"], G, None),
        ("dependency", "architect", [], "bump Django from 4.2 to 5.0 in requirements.txt", "requirements.txt pins Django 5.0", ["requirements.txt"], G, None),
        ("dependency", "lead", ["project"], "upgrade every dependency and fix whatever breaks", "", [], R, "vague"),
        ("git_op", "lead", ["tags"], "rebase the tags branch on main and resolve conflicts", "", [], R, "operational"),
        ("vague", "task", ["posts", "blog/posts/views.py"], "make the views more pythonic", "", [], R, "vague"),
    ],
    "An existing Go microservice for payments (internal/, go test): fix bugs and add idempotency.": [
        ("bugfix", "task", ["charges", "internal/charges/charge.go"], "fix Charge() in internal/charges/charge.go double-charging when the provider times out", "a retried charge with the same key charges once", ["internal/charges/charge.go", "internal/charges/charge_test.go"], G, None),
        ("new_feature", "architect", [], "design the idempotency component in internal/idempotency/: store request keys and replay stored responses", "a repeated request returns the stored response", [], B, None),
        ("new_feature", "task", ["idempotency", "internal/idempotency/store.go"], "implement Lookup(key string) (Response, bool) in internal/idempotency/store.go", "an unknown key returns false", ["internal/idempotency/store.go", "internal/idempotency/store_test.go"], G, None),
        ("modify", "task", ["http", "internal/http/middleware.go"], "change the logging middleware in internal/http/middleware.go to include the request id", "each log line has request_id", ["internal/http/middleware.go", "internal/http/middleware_test.go"], G, None),
        ("test_only", "task", ["charges", "internal/charges/charge_test.go"], "add a table-driven test for Refund() covering partial and full refunds", "both cases pass", ["internal/charges/charge_test.go"], G, None),
        ("investigation", "task", ["charges", "internal/charges/charge.go"], "find out why Charge() in internal/charges/charge.go sometimes returns nil error with a failed status and record the cause", "the cause is written in context", ["internal/charges/charge.go"], G, None),
        ("investigation", "architect", [], "investigate why the service is slow", "", [], R, "vague"),
        ("ci", "lead", ["project"], "write .github/workflows/test.yml running go vet and go test ./... on pull requests", "the workflow has both steps", [".github/workflows/test.yml"], G, None),
        ("deploy_op", "architect", [], "roll back the last deploy on the payments cluster", "", [], R, "operational"),
        ("refactor", "lead", ["charges"], "rewrite internal/charges/: split providers, retries, webhooks, refunds, disputes and reporting into packages", "charges is split up", ["internal/charges/"], B, None),
    ],
    "An existing React + Vite admin dashboard (src/, Vitest): add dark mode and fix bugs.": [
        ("new_feature", "architect", [], "design the theme component in src/theme/: light and dark themes with a toggle that persists", "the chosen theme survives a reload", [], B, None),
        ("new_feature", "task", ["theme", "src/theme/storage.ts"], "implement loadTheme(): 'light' | 'dark' in src/theme/storage.ts defaulting to light", "loadTheme() with empty storage returns 'light'", ["src/theme/storage.ts", "src/theme/storage.test.ts"], G, None),
        ("modify", "task", ["layout", "src/layout/Header.tsx"], "change the Header component in src/layout/Header.tsx to render the ThemeToggle", "Header renders a toggle button", ["src/layout/Header.tsx", "src/layout/Header.test.tsx"], G, None),
        ("bugfix", "task", ["users", "src/users/UserTable.tsx"], "fix the UserTable in src/users/UserTable.tsx crashing when a user has no email", "a user without email renders a dash", ["src/users/UserTable.tsx", "src/users/UserTable.test.tsx"], G, None),
        ("config", "architect", [], "enable strict mode in tsconfig.json", "tsconfig.json has strict: true", ["tsconfig.json"], G, None),
        ("refactor", "task", ["users", "src/users/UserTable.tsx"], "rewrite UserTable in src/users/UserTable.tsx: server pagination, sorting, filters, bulk actions, virtualisation and CSV export", "the table is rewritten", ["src/users/UserTable.tsx"], B, None),
        ("test_only", "lead", ["users"], "write src/users/format.test.ts covering formatLastSeen for today, yesterday and older dates", "three cases pass", ["src/users/format.test.ts"], G, None),
        ("git_op", "task", ["theme", "src/theme/storage.ts"], "tag a release with git tag v2.1.0 and push the tag", "", [], R, "operational"),
        ("vague", "architect", [], "modernise the UI", "", [], R, "vague"),
        ("performance", "lead", ["users"], "make everything faster", "", [], R, "vague"),
    ],
    "An existing Python data pipeline (pipeline/, pytest) that loads CSV exports into Postgres: add validation.": [
        ("new_feature", "lead", ["validation"], "scaffold pipeline/validate.py: check_required, check_types, check_ranges and validate_row", "all stubs implemented and tested", ["pipeline/validate.py", "tests/test_validate.py"], B, None),
        ("new_feature", "task", ["validation", "pipeline/validate.py"], "implement check_required(row: dict, fields: list[str]) -> list[str] in pipeline/validate.py returning missing fields", "a row without 'id' returns ['id']", ["pipeline/validate.py", "tests/test_validate.py"], G, None),
        ("modify", "task", ["load", "pipeline/load.py"], "change load_rows() in pipeline/load.py to skip rows that fail validate_row and count them", "an invalid row is skipped and counted", ["pipeline/load.py", "tests/test_load.py"], G, None),
        ("bugfix", "task", ["extract", "pipeline/extract.py"], "fix read_csv() in pipeline/extract.py mis-parsing quoted commas", "a quoted 'a, b' field stays one value", ["pipeline/extract.py", "tests/test_extract.py"], G, None),
        ("performance", "task", ["load", "pipeline/load.py"], "change insert_rows() in pipeline/load.py to use COPY instead of one INSERT per row", "10,000 rows load in one COPY", ["pipeline/load.py", "tests/test_load.py"], G, None),
        ("docs", "lead", ["project"], "write docs/validation.md describing every validation rule", "docs/validation.md lists each rule", ["docs/validation.md"], G, None),
        ("config", "architect", [], "set the batch size to 5000 in pipeline/config.yaml", "batch_size: 5000", ["pipeline/config.yaml"], G, None),
        ("refactor", "architect", [], "port the whole pipeline from pandas to polars", "every stage uses polars", [], B, None),
        ("deploy_op", "lead", ["load"], "trigger the Airflow DAG manually and check the run", "", [], R, "operational"),
        ("vague", "task", ["load", "pipeline/load.py"], "clean up the loading code", "", [], R, "vague"),
    ],
    "An existing Rust CLI for image conversion (src/, cargo test): add WebP output and a --quality flag.": [
        ("new_feature", "architect", [], "design the WebP output component in src/webp/: encode images to WebP with a quality setting", "a PNG converts to a valid WebP", [], B, None),
        ("new_feature", "task", ["webp", "src/webp/mod.rs"], "implement encode_webp(img: &DynamicImage, quality: u8) -> Vec<u8> in src/webp/mod.rs", "the output starts with the RIFF header", ["src/webp/mod.rs", "tests/webp.rs"], G, None),
        ("modify", "task", ["cli", "src/cli.rs"], "change parse_args() in src/cli.rs to accept --quality between 1 and 100", "--quality 0 is rejected", ["src/cli.rs", "tests/cli.rs"], G, None),
        ("modify", "lead", ["cli"], "mark src/formats.rs: add a WebP variant to the existing OutputFormat enum", "OutputFormat::WebP exists", ["src/formats.rs"], G, None),
        ("dependency", "architect", [], "add the webp crate to Cargo.toml", "Cargo.toml lists webp", ["Cargo.toml"], G, None),
        ("test_only", "task", ["cli", "tests/cli.rs"], "add an integration test converting tests/fixtures/logo.png to WebP", "the test produces a .webp file", ["tests/cli.rs"], G, None),
        ("bugfix", "task", ["convert", "src/convert.rs"], "fix convert() in src/convert.rs dropping the alpha channel when writing PNG", "a transparent pixel stays transparent", ["src/convert.rs", "tests/convert.rs"], G, None),
        ("refactor", "task", ["convert", "src/convert.rs"], "restructure src/convert.rs: plugin system for formats, streaming IO, parallelism, progress reporting and error types", "convert is restructured", ["src/convert.rs"], B, None),
        ("deploy_op", "task", ["project", "Cargo.toml"], "cargo publish the new version to crates.io", "", [], R, "operational"),
        ("vague", "lead", ["convert"], "improve image quality", "", [], R, "vague"),
    ],
}

TEST_APPS = {
    "An existing Flask inventory app (inventory/, pytest): add barcode lookup and fix stock bugs.": [
        ("new_feature", "architect", [], "design the barcode component in inventory/barcode/: look up items by barcode", "scanning a known barcode finds the item", [], B, None),
        ("new_feature", "task", ["barcode", "inventory/barcode/lookup.py"], "implement find_by_barcode(code: str) -> Item | None in inventory/barcode/lookup.py", "an unknown code returns None", ["inventory/barcode/lookup.py", "tests/test_lookup.py"], G, None),
        ("bugfix", "task", ["stock", "inventory/stock.py"], "fix adjust_stock() in inventory/stock.py allowing stock to go negative", "adjusting 3 below zero raises ValueError", ["inventory/stock.py", "tests/test_stock.py"], G, None),
        ("modify", "task", ["stock", "inventory/stock.py"], "change low_stock() in inventory/stock.py to use each item's own threshold", "an item under its threshold is listed", ["inventory/stock.py", "tests/test_stock.py"], G, None),
        ("migration", "lead", ["barcode"], "write the alembic migration adding a unique barcode column to items", "alembic upgrade head adds the column", ["migrations/versions/0005_barcode.py"], G, None),
        ("docker", "lead", ["project"], "write docker-compose.yml with the app and a Postgres service", "docker compose config validates", ["docker-compose.yml"], G, None),
        ("docker", "task", ["project", "docker-compose.yml"], "docker compose up and open the app", "", [], R, "operational"),
        ("refactor", "architect", [], "migrate the whole app from Flask to FastAPI", "every route runs on FastAPI", [], B, None),
        ("git_op", "lead", ["stock"], "squash the commits and merge the branch", "", [], R, "operational"),
        ("vague", "task", ["stock", "inventory/stock.py"], "make stock handling robust", "", [], R, "vague"),
    ],
    "An existing Next.js SaaS dashboard (app/, Playwright + Jest): add billing and fix auth.": [
        ("new_feature", "architect", [], "design the billing component in app/billing/: plans page, Stripe checkout and a webhook route", "a user can upgrade their plan", [], B, None),
        ("new_feature", "lead", ["billing"], "scaffold lib/billing.ts: createCheckoutSession, handleWebhook, currentPlan and cancelPlan", "all stubs implemented and tested", ["lib/billing.ts", "tests/billing.test.ts"], B, None),
        ("security", "task", ["auth", "lib/auth.ts"], "fix verifySession() in lib/auth.ts accepting expired cookies", "an expired cookie is rejected", ["lib/auth.ts", "tests/auth.test.ts"], G, None),
        ("modify", "task", ["dashboard", "app/dashboard/page.tsx"], "change the dashboard page in app/dashboard/page.tsx to show the current plan badge", "the page renders the plan name", ["app/dashboard/page.tsx", "tests/dashboard.test.tsx"], G, None),
        ("config", "architect", [], "add the STRIPE_WEBHOOK_SECRET key to .env.example", ".env.example lists STRIPE_WEBHOOK_SECRET", [".env.example"], G, None),
        ("ci", "lead", ["project"], "write .github/workflows/e2e.yml running the Playwright suite on pull requests", "the workflow runs npx playwright test", [".github/workflows/e2e.yml"], G, None),
        ("test_only", "task", ["billing", "tests/billing.test.ts"], "add a unit test for currentPlan() returning 'free' for a user with no subscription", "the test passes", ["tests/billing.test.ts"], G, None),
        ("refactor", "task", ["auth", "lib/auth.ts"], "rewrite lib/auth.ts: sessions, OAuth providers, magic links, 2FA, rate limiting and audit logging", "auth is rewritten", ["lib/auth.ts"], B, None),
        ("deploy_op", "architect", [], "promote the preview deployment to production on Vercel", "", [], R, "operational"),
        ("vague", "lead", ["dashboard"], "make the dashboard nicer", "", [], R, "vague"),
    ],
    "An existing Python library for parsing dates (dateparse/, pytest): add timezone support and tidy up.": [
        ("new_feature", "lead", ["timezones"], "scaffold dateparse/tz.py: parse_offset, to_utc and localise", "all stubs implemented and tested", ["dateparse/tz.py", "tests/test_tz.py"], B, None),
        ("new_feature", "task", ["timezones", "dateparse/tz.py"], "implement parse_offset(text: str) -> timedelta in dateparse/tz.py for '+05:30' style offsets", "parse_offset('+05:30') == timedelta(hours=5, minutes=30)", ["dateparse/tz.py", "tests/test_tz.py"], G, None),
        ("modify", "task", ["core", "dateparse/core.py"], "change parse() in dateparse/core.py to accept an optional tz argument", "parse('2024-01-01', tz='UTC') is aware", ["dateparse/core.py", "tests/test_core.py"], G, None),
        ("delete", "task", ["core", "dateparse/core.py"], "delete the deprecated parse_legacy() from dateparse/core.py after checking nothing calls it", "parse_legacy is gone", ["dateparse/core.py"], G, None),
        ("bugfix", "task", ["core", "dateparse/core.py"], "fix parse() in dateparse/core.py rejecting February 29 in leap years", "parse('2024-02-29') succeeds", ["dateparse/core.py", "tests/test_core.py"], G, None),
        ("docs", "architect", [], "document timezone support in README.md with two examples", "README.md has a Timezones section", ["README.md"], G, None),
        ("dependency", "architect", [], "drop Python 3.8 support by setting requires-python to >=3.10 in pyproject.toml", "requires-python is >=3.10", ["pyproject.toml"], G, None),
        ("investigation", "lead", ["core"], "look into the parsing weirdness", "", [], R, "vague"),
        ("deploy_op", "lead", ["project"], "build the wheel and upload it to PyPI with twine", "", [], R, "operational"),
        ("refactor", "architect", [], "rewrite the parser as a proper grammar with a tokenizer, parser, AST and error recovery", "the new parser handles every existing format", [], B, None),
    ],
}


def rows(apps):
    for goal, items in apps.items():
        for case, level, path, node, done_when, files, label, reason in items:
            yield {"goal": goal, "level": level, "path": path, "node": node, "done_when": done_when,
                   "files": files, "label": label, "redo_reason": reason, "source": "coding_cases",
                   "case": case}


def write(path, new):
    existing = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    kept = [r for r in existing if r.get("source") != "coding_cases"]
    with path.open("w", encoding="utf-8") as f:
        for r in kept + new:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(kept)


if __name__ == "__main__":
    assert not set(TRAIN_APPS) & set(TEST_APPS)
    from collections import Counter
    for name, apps in (("train", TRAIN_APPS), ("test", TEST_APPS)):
        new = list(rows(apps))
        kept = write(HERE / f"{name}.jsonl", new)
        print(f"{name}.jsonl: {kept} existing + {len(new)} coding cases = {kept + len(new)} | "
              f"{dict(Counter(r['label'] for r in new))}")
    total = sum(len(v) for v in TRAIN_APPS.values()) + sum(len(v) for v in TEST_APPS.values())
    print(f"{total} coding-case rows; case types: "
          f"{dict(Counter(r[0] for apps in (TRAIN_APPS, TEST_APPS) for v in apps.values() for r in v))}")
