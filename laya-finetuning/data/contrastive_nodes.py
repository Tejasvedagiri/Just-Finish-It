"""Contrastive training rows (train.jsonl only; test.jsonl is never touched).

Written after the first fine-tunes' confusion matrix showed the judge's main
weakness: BREAKDOWN nodes (a whole component, a file with several functions,
a function that bundles many steps) called GOOD -- 16 of 38 on the held-out
set. Every app here therefore comes in matched pairs at each level -- one
small/single thing (GOOD) next to one multi-part thing (BREAKDOWN) -- plus
operational and vague REDOs phrased differently from the earlier rows
(launch, spin up, deploy, curl, tail, refactor, optimise, ...).

    python laya-finetuning/data/contrastive_nodes.py   # (re)writes its rows in train.jsonl
Row tuple: (level, path, node, done_when, files, label, redo_reason)
"""
import json
from pathlib import Path

B, G, R = "BREAKDOWN", "GOOD", "REDO"
HERE = Path(__file__).resolve().parent

APPS = {
    "An invoicing API in FastAPI: customers, invoices, line items and PDF export.": [
        ("architect", [], "design the invoices component in app/invoices/: invoice model, totals and PDF export", "an invoice with two lines totals correctly", [], B, None),
        ("architect", [], "change the default currency constant in app/settings.py from USD to EUR", "DEFAULT_CURRENCY == 'EUR'", ["app/settings.py"], G, None),
        ("lead", ["invoices"], "scaffold app/invoices/totals.py: line_total, subtotal, tax and grand_total", "all stubs implemented and tested", ["app/invoices/totals.py", "tests/test_totals.py"], B, None),
        ("lead", ["invoices"], "scaffold app/invoices/constants.py with the single TAX_RATE constant", "TAX_RATE == 0.2", ["app/invoices/constants.py"], G, None),
        ("task", ["invoices", "app/invoices/totals.py"], "implement line_total(qty: int, price: Decimal) -> Decimal in app/invoices/totals.py", "line_total(3, Decimal('2.50')) == Decimal('7.50')", ["app/invoices/totals.py", "tests/test_totals.py"], G, None),
        ("task", ["invoices", "app/invoices/pdf.py"], "implement export_pdf(invoice) in app/invoices/pdf.py: load fonts, lay out the header, render every line, add totals, footer, page numbers and save", "the PDF is produced", ["app/invoices/pdf.py"], B, None),
        ("task", ["invoices", "app/invoices/pdf.py"], "launch uvicorn and download an invoice PDF to check it", "", [], R, "operational"),
        ("lead", ["customers"], "refactor as needed", "", [], R, "vague"),
    ],
    "A Go CLI that checks a list of URLs and reports which are down.": [
        ("architect", [], "design the checker component in internal/check/: request each URL with a timeout and classify the result", "a down URL is reported as down", [], B, None),
        ("architect", [], "change the default timeout in internal/config/config.go from 5s to 10s", "DefaultTimeout == 10*time.Second", ["internal/config/config.go"], G, None),
        ("lead", ["checker"], "scaffold internal/check/check.go: CheckURL, Classify, CheckAll and Summary", "all stubs implemented and tested", ["internal/check/check.go", "internal/check/check_test.go"], B, None),
        ("lead", ["checker"], "scaffold internal/check/status.go with the single Status type and its three constants", "Up, Down and Slow are defined", ["internal/check/status.go"], G, None),
        ("task", ["checker", "internal/check/check.go"], "implement Classify(code int, err error) Status in internal/check/check.go", "Classify(503, nil) == Down", ["internal/check/check.go", "internal/check/check_test.go"], G, None),
        ("task", ["checker", "internal/check/check.go"], "implement CheckAll(urls []string) in internal/check/check.go: worker pool, timeouts, retries, rate limiting, progress bar and a JSON report", "it checks everything", ["internal/check/check.go"], B, None),
        ("architect", [], "go build and run the binary against urls.txt", "", [], R, "operational"),
        ("task", ["report", "internal/report/report.go"], "optimise the report", "", [], R, "vague"),
    ],
    "A Flask recipe API with ingredients, search and ratings.": [
        ("architect", [], "design the search component in recipes/search/: search recipes by name and ingredient", "searching 'egg' finds the omelette", [], B, None),
        ("architect", [], "rename the API title in recipes/config.py to 'Recipe API'", "API_TITLE == 'Recipe API'", ["recipes/config.py"], G, None),
        ("lead", ["search"], "scaffold recipes/search/query.py: normalise_term, match_name, match_ingredient and search", "all stubs implemented and tested", ["recipes/search/query.py", "tests/test_query.py"], B, None),
        ("lead", ["search"], "scaffold recipes/search/__init__.py re-exporting search", "from recipes.search import search works", ["recipes/search/__init__.py"], G, None),
        ("task", ["search", "recipes/search/query.py"], "implement normalise_term(term: str) -> str in recipes/search/query.py lowercasing and trimming", "normalise_term(' Egg ') == 'egg'", ["recipes/search/query.py", "tests/test_query.py"], G, None),
        ("task", ["ratings", "recipes/ratings/service.py"], "implement the ratings service in recipes/ratings/service.py: add, update, delete, average, histogram and spam detection", "ratings work", ["recipes/ratings/service.py"], B, None),
        ("lead", ["search"], "curl http://localhost:5000/search?q=egg and read the response", "", [], R, "operational"),
        ("architect", [], "clean everything up", "", [], R, "vague"),
    ],
    "A Node service that streams live stock prices to clients over Server-Sent Events.": [
        ("architect", [], "design the price feed component in src/feed/: poll the quote API and publish changes", "a price change is published", [], B, None),
        ("architect", [], "change the poll interval constant in src/config.js from 5000 to 2000", "POLL_MS === 2000", ["src/config.js"], G, None),
        ("lead", ["price feed"], "scaffold src/feed/poller.js: fetchQuotes, diffQuotes, publish and start", "all stubs implemented and tested", ["src/feed/poller.js", "test/poller.test.js"], B, None),
        ("lead", ["price feed"], "scaffold src/feed/symbols.js exporting the single SYMBOLS array", "SYMBOLS lists AAPL and MSFT", ["src/feed/symbols.js"], G, None),
        ("task", ["price feed", "src/feed/poller.js"], "implement diffQuotes(prev, next) in src/feed/poller.js returning only changed symbols", "a changed AAPL price is returned alone", ["src/feed/poller.js", "test/poller.test.js"], G, None),
        ("task", ["sse", "src/sse/server.js"], "implement the SSE server in src/sse/server.js: connections, heartbeats, reconnects, backpressure, auth and metrics", "streaming works", ["src/sse/server.js"], B, None),
        ("task", ["sse", "src/sse/server.js"], "spin up the server with node src/index.js and watch the stream", "", [], R, "operational"),
        ("lead", ["sse"], "handle the streaming side", "", [], R, "vague"),
    ],
    "A Python chess engine with move generation and a simple minimax search.": [
        ("architect", [], "design the move generator in chess/moves/: legal moves for every piece", "the start position has 20 legal moves", [], B, None),
        ("architect", [], "change the default search depth in chess/config.py from 3 to 4", "SEARCH_DEPTH == 4", ["chess/config.py"], G, None),
        ("lead", ["move generator"], "scaffold chess/moves/pieces.py: pawn_moves, knight_moves, bishop_moves, rook_moves, queen_moves and king_moves", "all stubs implemented and tested", ["chess/moves/pieces.py", "tests/test_pieces.py"], B, None),
        ("lead", ["move generator"], "scaffold chess/moves/directions.py with the single KNIGHT_OFFSETS constant", "KNIGHT_OFFSETS has 8 entries", ["chess/moves/directions.py"], G, None),
        ("task", ["move generator", "chess/moves/pieces.py"], "implement knight_moves(square: int) -> list[int] in chess/moves/pieces.py", "a knight on b1 has 3 moves", ["chess/moves/pieces.py", "tests/test_pieces.py"], G, None),
        ("task", ["search", "chess/search.py"], "implement the engine in chess/search.py: minimax, alpha-beta, move ordering, transposition table, quiescence and time control", "it plays", ["chess/search.py"], B, None),
        ("lead", ["search"], "play a game against it with python -m chess", "", [], R, "operational"),
        ("task", ["search", "chess/search.py"], "make it play stronger", "", [], R, "vague"),
    ],
    "A Vue photo gallery with albums, lightbox viewing and uploads.": [
        ("architect", [], "design the albums component in src/albums/: list albums and their photos", "an album shows its photos", [], B, None),
        ("architect", [], "change the thumbnail size constant in src/config.ts from 150 to 200", "THUMB_SIZE === 200", ["src/config.ts"], G, None),
        ("lead", ["albums"], "scaffold src/albums/AlbumGrid.vue: props, the grid template, the click handler and lazy loading", "all stubs implemented and tested", ["src/albums/AlbumGrid.vue", "tests/AlbumGrid.spec.ts"], B, None),
        ("lead", ["albums"], "scaffold src/albums/types.ts with the single Album interface", "Album has id, title and photos", ["src/albums/types.ts"], G, None),
        ("task", ["albums", "src/albums/format.ts"], "implement photoCount(album: Album): string in src/albums/format.ts", "an album with 1 photo reads '1 photo'", ["src/albums/format.ts", "tests/format.spec.ts"], G, None),
        ("task", ["uploads", "src/uploads/Uploader.vue"], "implement the Uploader in src/uploads/Uploader.vue: drag and drop, previews, resizing, progress, retries and cancel", "uploads work", ["src/uploads/Uploader.vue"], B, None),
        ("architect", [], "npm run dev and upload a few photos", "", [], R, "operational"),
        ("lead", ["uploads"], "polish the uploads", "", [], R, "vague"),
    ],
    "A Django expense tracker with categories, monthly budgets and charts.": [
        ("architect", [], "design the budgets component in expenses/budgets/: monthly limits per category and overspend checks", "overspending a category is detected", [], B, None),
        ("architect", [], "bump the app version in expenses/__init__.py to 2.0.0", "__version__ == '2.0.0'", ["expenses/__init__.py"], G, None),
        ("lead", ["budgets"], "scaffold expenses/budgets/services.py: spent_this_month, remaining, is_over_budget and monthly_report", "all stubs implemented and tested", ["expenses/budgets/services.py", "tests/test_budgets.py"], B, None),
        ("lead", ["budgets"], "scaffold expenses/budgets/apps.py with the single BudgetsConfig class", "BudgetsConfig.name == 'expenses.budgets'", ["expenses/budgets/apps.py"], G, None),
        ("task", ["budgets", "expenses/budgets/services.py"], "implement remaining(limit: Decimal, spent: Decimal) -> Decimal in expenses/budgets/services.py never going below zero", "remaining(100, 130) == 0", ["expenses/budgets/services.py", "tests/test_budgets.py"], G, None),
        ("task", ["charts", "expenses/charts/views.py"], "implement the charts view in expenses/charts/views.py: query, group by month and category, fill gaps, compute trends, render three chart types and export CSV", "charts work", ["expenses/charts/views.py"], B, None),
        ("task", ["charts", "expenses/charts/views.py"], "python manage.py createsuperuser and log in to see the charts", "", [], R, "operational"),
        ("architect", [], "make budgeting smarter", "", [], R, "vague"),
    ],
    "A Python CLI that generates QR codes from text or URLs and saves them as PNG.": [
        ("architect", [], "design the encoder component in qrgen/encode.py: turn text into a QR matrix", "'hello' becomes a valid matrix", [], B, None),
        ("architect", [], "change the default output file name in qrgen/defaults.py to 'qr.png'", "OUTPUT == 'qr.png'", ["qrgen/defaults.py"], G, None),
        ("lead", ["encoder"], "scaffold qrgen/encode.py: choose_version, encode_data, add_error_correction and build_matrix", "all stubs implemented and tested", ["qrgen/encode.py", "tests/test_encode.py"], B, None),
        ("lead", ["project"], "scaffold qrgen/__main__.py that only calls cli.main()", "python -m qrgen runs the CLI", ["qrgen/__main__.py"], G, None),
        ("task", ["cli", "qrgen/cli.py"], "implement parse_size(value: str) -> int in qrgen/cli.py rejecting sizes under 21", "parse_size('10') raises ValueError", ["qrgen/cli.py", "tests/test_cli.py"], G, None),
        ("task", ["encoder", "qrgen/encode.py"], "implement build_matrix in qrgen/encode.py: finder patterns, timing, alignment, format bits, data placement, masking and penalty scoring", "the matrix is valid", ["qrgen/encode.py"], B, None),
        ("lead", ["cli"], "pip install -e . and run qrgen 'hello'", "", [], R, "operational"),
        ("task", ["render", "qrgen/render.py"], "improve the output quality", "", [], R, "vague"),
    ],
    "A Rust log shipper that tails files and forwards lines to an HTTP collector.": [
        ("architect", [], "design the tailer component in src/tail/: follow files and survive rotation", "new lines are read after rotation", [], B, None),
        ("architect", [], "change the default batch size in src/config.rs from 100 to 500", "DEFAULT_BATCH == 500", ["src/config.rs"], G, None),
        ("lead", ["tailer"], "scaffold src/tail/mod.rs: open_at_end, read_new_lines, detect_rotation and reopen", "all stubs implemented and tested", ["src/tail/mod.rs", "tests/tail.rs"], B, None),
        ("lead", ["project"], "scaffold src/version.rs with the single VERSION constant", "VERSION is exported", ["src/version.rs"], G, None),
        ("task", ["tailer", "src/tail/mod.rs"], "implement detect_rotation(prev_inode: u64, cur_inode: u64) -> bool in src/tail/mod.rs", "different inodes mean rotated", ["src/tail/mod.rs", "tests/tail.rs"], G, None),
        ("task", ["sender", "src/send/mod.rs"], "implement the sender in src/send/mod.rs: batching, gzip, retries with backoff, disk buffering, TLS and metrics", "shipping works", ["src/send/mod.rs"], B, None),
        ("task", ["sender", "src/send/mod.rs"], "cargo run -- /var/log/app.log and tail the collector logs", "", [], R, "operational"),
        ("lead", ["sender"], "make shipping reliable", "", [], R, "vague"),
    ],
    "A Rails newsletter service with subscribers, campaigns and open tracking.": [
        ("architect", [], "design the campaigns component in app/campaigns/: draft, schedule and send campaigns", "a scheduled campaign is sent", [], B, None),
        ("architect", [], "change the sender address in config/newsletter.yml to news@example.com", "the sender is news@example.com", ["config/newsletter.yml"], G, None),
        ("lead", ["campaigns"], "scaffold app/services/campaign_sender.rb: recipients, render, deliver and record_send", "all stubs implemented and tested", ["app/services/campaign_sender.rb", "spec/campaign_sender_spec.rb"], B, None),
        ("lead", ["campaigns"], "scaffold app/models/campaign_status.rb with the single STATUSES constant", "STATUSES lists draft, scheduled and sent", ["app/models/campaign_status.rb"], G, None),
        ("task", ["campaigns", "app/services/campaign_sender.rb"], "implement recipients(campaign) in app/services/campaign_sender.rb returning only confirmed subscribers", "an unconfirmed subscriber is excluded", ["app/services/campaign_sender.rb", "spec/campaign_sender_spec.rb"], G, None),
        ("task", ["tracking", "app/services/open_tracker.rb"], "implement open tracking in app/services/open_tracker.rb: pixel, click links, bot filtering, dedupe, reports and unsubscribe", "tracking works", ["app/services/open_tracker.rb"], B, None),
        ("architect", [], "bundle exec rails server and send a test campaign", "", [], R, "operational"),
        ("task", ["tracking", "app/services/open_tracker.rb"], "fix up tracking", "", [], R, "vague"),
    ],
    "A Python task queue using Redis: enqueue jobs, workers, retries and a status API.": [
        ("architect", [], "design the worker component in tq/worker/: fetch jobs, run them and record results", "a job runs and its result is stored", [], B, None),
        ("architect", [], "change the default queue name in tq/settings.py to 'default'", "QUEUE == 'default'", ["tq/settings.py"], G, None),
        ("lead", ["worker"], "scaffold tq/worker/loop.py: fetch_job, run_job, record_result and run_forever", "all stubs implemented and tested", ["tq/worker/loop.py", "tests/test_loop.py"], B, None),
        ("lead", ["worker"], "scaffold tq/worker/errors.py with the single JobFailed exception", "JobFailed is raisable", ["tq/worker/errors.py"], G, None),
        ("task", ["worker", "tq/worker/retry.py"], "implement backoff(attempt: int) -> float in tq/worker/retry.py doubling from 1 second", "backoff(3) == 8.0", ["tq/worker/retry.py", "tests/test_retry.py"], G, None),
        ("task", ["worker", "tq/worker/loop.py"], "implement run_forever in tq/worker/loop.py: poll, lease, heartbeats, retries, dead-lettering, graceful shutdown and logging", "the worker runs", ["tq/worker/loop.py"], B, None),
        ("lead", ["worker"], "docker compose up redis and start two workers", "", [], R, "operational"),
        ("architect", [], "make the queue scalable", "", [], R, "vague"),
    ],
    "A Node service that syncs Google Calendar events into a local SQLite database.": [
        ("architect", [], "design the sync component in src/sync/: fetch changed events and upsert them", "a changed event is updated locally", [], B, None),
        ("architect", [], "change the sync window constant in src/config.js from 30 to 60 days", "SYNC_DAYS === 60", ["src/config.js"], G, None),
        ("lead", ["sync"], "scaffold src/sync/engine.js: fetchChanges, toRow, upsertEvents and saveSyncToken", "all stubs implemented and tested", ["src/sync/engine.js", "test/engine.test.js"], B, None),
        ("lead", ["project"], "scaffold .env.example with the single GOOGLE_CLIENT_ID setting", ".env.example lists GOOGLE_CLIENT_ID", [".env.example"], G, None),
        ("task", ["sync", "src/sync/engine.js"], "implement toRow(event) in src/sync/engine.js mapping an API event to a table row", "an all-day event maps start to a date", ["src/sync/engine.js", "test/engine.test.js"], G, None),
        ("task", ["auth", "src/auth/oauth.js"], "implement OAuth in src/auth/oauth.js: consent URL, callback, token exchange, refresh, storage, revocation and scopes", "auth works", ["src/auth/oauth.js"], B, None),
        ("task", ["auth", "src/auth/oauth.js"], "open the consent URL in a browser and log in", "", [], R, "operational"),
        ("lead", ["auth"], "handle auth", "", [], R, "vague"),
    ],
    "A Go password manager CLI that stores entries encrypted in a local vault file.": [
        ("architect", [], "design the vault component in internal/vault/: load, save and encrypt the entry file", "a saved vault reloads with the same entries", [], B, None),
        ("architect", [], "change the default vault path in internal/config/config.go to ~/.pwvault", "DefaultPath ends with .pwvault", ["internal/config/config.go"], G, None),
        ("lead", ["vault"], "scaffold internal/vault/crypto.go: DeriveKey, Encrypt, Decrypt and NewNonce", "all stubs implemented and tested", ["internal/vault/crypto.go", "internal/vault/crypto_test.go"], B, None),
        ("lead", ["vault"], "scaffold internal/vault/entry.go with the single Entry struct", "Entry has Name, User and Password", ["internal/vault/entry.go"], G, None),
        ("task", ["vault", "internal/vault/crypto.go"], "implement NewNonce() ([]byte, error) in internal/vault/crypto.go returning 12 random bytes", "the nonce is 12 bytes long", ["internal/vault/crypto.go", "internal/vault/crypto_test.go"], G, None),
        ("task", ["cli", "cmd/pw/main.go"], "implement the whole CLI in cmd/pw/main.go: add, get, list, delete, generate, copy to clipboard, change master password and import", "the CLI works", ["cmd/pw/main.go"], B, None),
        ("architect", [], "go install ./cmd/pw and add a test entry", "", [], R, "operational"),
        ("task", ["cli", "cmd/pw/main.go"], "tighten security", "", [], R, "vague"),
    ],
    "A Svelte survey builder: create questions, share a link and view results.": [
        ("architect", [], "design the builder component in src/builder/: add, edit and reorder questions", "questions can be added and reordered", [], B, None),
        ("architect", [], "change the max questions constant in src/config.ts from 20 to 50", "MAX_QUESTIONS === 50", ["src/config.ts"], G, None),
        ("lead", ["builder"], "scaffold src/builder/store.ts: addQuestion, updateQuestion, removeQuestion and moveQuestion", "all stubs implemented and tested", ["src/builder/store.ts", "tests/store.test.ts"], B, None),
        ("lead", ["builder"], "scaffold src/builder/types.ts with the single Question type", "Question has id, text and kind", ["src/builder/types.ts"], G, None),
        ("task", ["builder", "src/builder/store.ts"], "implement moveQuestion(list, from: number, to: number) in src/builder/store.ts", "moving index 0 to 2 reorders the list", ["src/builder/store.ts", "tests/store.test.ts"], G, None),
        ("task", ["results", "src/results/Results.svelte"], "implement the Results page in src/results/Results.svelte: fetch answers, aggregate, charts per question, filters, CSV export and sharing", "results show", ["src/results/Results.svelte"], B, None),
        ("lead", ["results"], "npm run preview and fill in the survey a few times", "", [], R, "operational"),
        ("architect", [], "make surveys more engaging", "", [], R, "vague"),
    ],
    "A Python collector that reads IoT sensor readings over MQTT and stores them in InfluxDB.": [
        ("architect", [], "design the ingest component in collector/ingest/: subscribe to topics and parse readings", "a published reading is parsed", [], B, None),
        ("architect", [], "change the MQTT topic prefix in collector/settings.py to 'sensors/'", "TOPIC_PREFIX == 'sensors/'", ["collector/settings.py"], G, None),
        ("lead", ["ingest"], "scaffold collector/ingest/parse.py: parse_topic, parse_payload, to_point and validate_reading", "all stubs implemented and tested", ["collector/ingest/parse.py", "tests/test_parse.py"], B, None),
        ("lead", ["ingest"], "scaffold collector/ingest/units.py with the single UNITS mapping", "UNITS['temp'] == 'C'", ["collector/ingest/units.py"], G, None),
        ("task", ["ingest", "collector/ingest/parse.py"], "implement parse_topic(topic: str) -> tuple[str, str] in collector/ingest/parse.py returning device and metric", "'sensors/k1/temp' gives ('k1', 'temp')", ["collector/ingest/parse.py", "tests/test_parse.py"], G, None),
        ("task", ["storage", "collector/storage/writer.py"], "implement the writer in collector/storage/writer.py: batching, retention policies, retries, buffering on disk, downsampling and alerts", "storage works", ["collector/storage/writer.py"], B, None),
        ("task", ["storage", "collector/storage/writer.py"], "mosquitto_pub a test reading and query Influx for it", "", [], R, "operational"),
        ("lead", ["storage"], "take care of storage", "", [], R, "vague"),
    ],
    "A Python tool that builds monthly PDF sales reports from a Postgres database.": [
        ("architect", [], "design the report component in reports/build/: query sales and lay out the PDF", "a monthly PDF is produced", [], B, None),
        ("architect", [], "change the report title in reports/config.py to 'Monthly Sales'", "TITLE == 'Monthly Sales'", ["reports/config.py"], G, None),
        ("lead", ["report"], "scaffold reports/build/queries.py: monthly_totals, top_customers, by_region and growth", "all stubs implemented and tested", ["reports/build/queries.py", "tests/test_queries.py"], B, None),
        ("lead", ["report"], "scaffold reports/build/fonts.py with the single FONT_PATH constant", "FONT_PATH points at the bundled font", ["reports/build/fonts.py"], G, None),
        ("task", ["report", "reports/build/queries.py"], "implement growth(prev: float, cur: float) -> float in reports/build/queries.py as a percentage", "growth(100, 120) == 20.0", ["reports/build/queries.py", "tests/test_queries.py"], G, None),
        ("task", ["report", "reports/build/layout.py"], "implement render_report in reports/build/layout.py: cover page, table of contents, four charts, tables, footnotes and page numbering", "the report renders", ["reports/build/layout.py"], B, None),
        ("architect", [], "schedule the report with a cron entry and wait for the first run", "", [], R, "operational"),
        ("task", ["report", "reports/build/layout.py"], "make the report prettier", "", [], R, "vague"),
    ],
    "An Express leaderboard API for a mobile game: submit scores and fetch rankings.": [
        ("architect", [], "design the rankings component in src/rankings/: store scores and compute ranks", "the highest score ranks first", [], B, None),
        ("architect", [], "change the leaderboard size constant in src/config.js from 10 to 100", "TOP_N === 100", ["src/config.js"], G, None),
        ("lead", ["rankings"], "scaffold src/rankings/service.js: submitScore, topN, rankOf and around", "all stubs implemented and tested", ["src/rankings/service.js", "test/rankings.test.js"], B, None),
        ("lead", ["rankings"], "scaffold src/rankings/keys.js exporting the single leaderboardKey function", "leaderboardKey('daily') returns a string", ["src/rankings/keys.js"], G, None),
        ("task", ["rankings", "src/rankings/service.js"], "implement rankOf(scores, player) in src/rankings/service.js returning a 1-based rank", "the top player has rank 1", ["src/rankings/service.js", "test/rankings.test.js"], G, None),
        ("task", ["anti-cheat", "src/anticheat/check.js"], "implement anti-cheat in src/anticheat/check.js: signatures, replay checks, rate limits, outlier detection, bans and appeals", "cheating is stopped", ["src/anticheat/check.js"], B, None),
        ("task", ["rankings", "src/rankings/service.js"], "deploy to Heroku and submit a score from the app", "", [], R, "operational"),
        ("lead", ["anti-cheat"], "stop the cheaters", "", [], R, "vague"),
    ],
    "A FastAPI wrapper around a translation model with caching and rate limits.": [
        ("architect", [], "design the cache component in app/cache/: cache translations by text and language pair", "a repeated request hits the cache", [], B, None),
        ("architect", [], "change the default target language in app/settings.py to 'fr'", "DEFAULT_TARGET == 'fr'", ["app/settings.py"], G, None),
        ("lead", ["cache"], "scaffold app/cache/store.py: make_key, get, put and evict_expired", "all stubs implemented and tested", ["app/cache/store.py", "tests/test_store.py"], B, None),
        ("lead", ["cache"], "scaffold app/cache/ttl.py with the single TTL_SECONDS constant", "TTL_SECONDS == 3600", ["app/cache/ttl.py"], G, None),
        ("task", ["cache", "app/cache/store.py"], "implement make_key(text: str, src: str, dst: str) -> str in app/cache/store.py", "the same inputs give the same key", ["app/cache/store.py", "tests/test_store.py"], G, None),
        ("task", ["api", "app/api/translate.py"], "implement the translate endpoint in app/api/translate.py: validation, language detection, chunking, caching, rate limiting, batching and streaming", "translation works", ["app/api/translate.py"], B, None),
        ("lead", ["api"], "download the model weights and start the server", "", [], R, "operational"),
        ("task", ["api", "app/api/translate.py"], "speed things up", "", [], R, "vague"),
    ],
    "A Python CLI that prints statistics about a git repository: commits per author, busiest days and file churn.": [
        ("architect", [], "design the history component in gitstats/history.py: read commits with their author, date and files", "commits are listed with authors", [], B, None),
        ("architect", [], "change the default top-N constant in gitstats/defaults.py from 5 to 10", "TOP_N == 10", ["gitstats/defaults.py"], G, None),
        ("lead", ["history"], "scaffold gitstats/stats.py: commits_per_author, busiest_weekday and file_churn", "all stubs implemented and tested", ["gitstats/stats.py", "tests/test_stats.py"], B, None),
        ("lead", ["project"], "scaffold gitstats/__main__.py that only calls cli.main()", "python -m gitstats runs", ["gitstats/__main__.py"], G, None),
        ("task", ["history", "gitstats/stats.py"], "implement busiest_weekday(dates: list[date]) -> str in gitstats/stats.py", "three Monday dates give 'Monday'", ["gitstats/stats.py", "tests/test_stats.py"], G, None),
        ("task", ["cli", "gitstats/cli.py"], "implement main() in gitstats/cli.py: arguments, repo discovery, date filters, all three reports, colours, JSON output and paging", "the CLI works", ["gitstats/cli.py"], B, None),
        ("task", ["cli", "gitstats/cli.py"], "run gitstats on this repo and eyeball the numbers", "", [], R, "operational"),
        ("architect", [], "give better insights", "", [], R, "vague"),
    ],
    "A Kotlin Android to-do app backend in Ktor with lists, items and sharing.": [
        ("architect", [], "design the sharing component in sharing/: invite users to a list and manage access", "an invited user can read the list", [], B, None),
        ("architect", [], "change the API version prefix in Application.kt from /v1 to /v2", "routes start with /v2", ["Application.kt"], G, None),
        ("lead", ["sharing"], "scaffold sharing/SharingService.kt: invite, accept, revoke and canRead", "all stubs implemented and tested", ["sharing/SharingService.kt", "sharing/SharingServiceTest.kt"], B, None),
        ("lead", ["sharing"], "scaffold sharing/Role.kt with the single Role enum", "Role has OWNER and VIEWER", ["sharing/Role.kt"], G, None),
        ("task", ["sharing", "sharing/SharingService.kt"], "implement canRead(user: String, list: TodoList): Boolean in sharing/SharingService.kt", "the owner can read their list", ["sharing/SharingService.kt", "sharing/SharingServiceTest.kt"], G, None),
        ("task", ["sync", "sync/SyncService.kt"], "implement offline sync in sync/SyncService.kt: change log, conflict resolution, batching, retries, push notifications and migrations", "sync works", ["sync/SyncService.kt"], B, None),
        ("lead", ["sync"], "./gradlew run and point the emulator at it", "", [], R, "operational"),
        ("task", ["sync", "sync/SyncService.kt"], "improve sync", "", [], R, "vague"),
    ],
}


def rows():
    for goal, items in APPS.items():
        for level, path, node, done_when, files, label, reason in items:
            yield {"goal": goal, "level": level, "path": path, "node": node, "done_when": done_when,
                   "files": files, "label": label, "redo_reason": reason, "source": "contrastive"}


if __name__ == "__main__":
    path = HERE / "train.jsonl"
    existing = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    kept = [r for r in existing if r.get("source") != "contrastive"]
    new = list(rows())
    test_goals = {json.loads(line)["goal"] for line in (HERE / "test.jsonl").read_text(encoding="utf-8").splitlines()
                  if line.strip()}
    assert not {r["goal"] for r in new} & test_goals, "a contrastive app overlaps the test set"
    with path.open("w", encoding="utf-8") as f:
        for r in kept + new:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    print(f"train.jsonl: {len(kept)} existing + {len(new)} contrastive = {len(kept) + len(new)} | "
          f"{dict(Counter(r['label'] for r in new))}")
