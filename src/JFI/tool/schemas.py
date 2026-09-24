# tools/schemas.py
#
# CORE_TOOLS are sent with EVERY request (see runner._tools_for_session) --
# the ones used almost every turn for real coding/verification work.
# DEFERRED_TOOLS' full schemas are withheld by default and only added to a
# session's own request once the model calls load_tool(name) to unlock one
# (see tool/deferred_tools.py and DEFERRED_TOOLS_RULES below) -- this
# mirrors this exact model's own experience of deferred tools it has to
# fetch definitions for before calling. Measured cost at the time this was
# added: ~2700 tokens total across 13 tools, resent unconditionally on
# EVERY turn for the life of a session (hundreds to low thousands of turns
# on a long-running one) regardless of whether that turn ever touches a
# browser or a video file -- DEFERRED_TOOLS alone were ~1180 of those
# tokens (browse_webpage/capture_screenshot/view_image/
# fetch_webpage_images/extract_video_frames), paid on every single turn of
# every session whether or not that session ever uses them.

CORE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Writes code or text to a file. Automatically creates directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the file, e.g., 'src/main.py'"
                    },
                    "content": {
                        "type": "string",
                        "description": "The exact content or code to write into the file."
                    }
                },
                "required": ["file_path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": (
                "Executes a shell command on the terminal and returns the output. "
                "If a `curl` request is blocked by a site's anti-bot/DoS protection "
                "(e.g. a Cloudflare challenge page instead of the real content) and the "
                "FLARESOLVERR_URL environment variable is set, retry the request through "
                "that FlareSolverr endpoint instead of curling the site directly -- POST "
                "JSON like {\"cmd\": \"request.get\", \"url\": \"<target-url>\", "
                "\"maxTimeout\": 60000} to it, e.g. "
                "curl -s -X POST \"$FLARESOLVERR_URL\" -H 'Content-Type: application/json' "
                "-d '{\"cmd\":\"request.get\",\"url\":\"<target-url>\",\"maxTimeout\":60000}'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The terminal command to run, e.g., 'pip install requests' or 'python test.py'"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Seconds to wait before giving up on this command. Defaults to 300 -- "
                            "raise it (e.g. 600-1200) for anything that legitimately takes even "
                            "longer, like installing large packages or running a slow build/test "
                            "suite."
                        )
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Reads the content of an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the file to read."
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "append_to_file",
            "description": (
                "Appends content to the end of a file, creating it if needed. Use this to build "
                "up a long document in several smaller calls instead of one oversized write_file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the file, e.g., 'notes.txt'"
                    },
                    "content": {
                        "type": "string",
                        "description": "The chunk of content to add at the end of the file."
                    }
                },
                "required": ["file_path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": (
                "Replaces one exact substring in a file, leaving the rest untouched. This is the "
                "correct way to tick a checkbox (e.g. '- [ ] 1.1 Foo' -> '- [x] 1.1 Foo') or to "
                "patch a few lines. Prefer this over rewriting a whole file with write_file. "
                "old_string must match exactly once, including whitespace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the file to edit."
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact existing text to replace. Must occur exactly once."
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The text to put in its place."
                    }
                },
                "required": ["file_path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "load_tool",
            "description": (
                "Unlocks one deferred tool's full schema starting on your NEXT turn, without "
                "paying its schema cost on every turn until you actually need it. See SAVED "
                "CONTEXT / the deferred-tools list in this system message for what's available "
                "and each one's one-line purpose -- call this once per tool name, then use it "
                "normally from then on for the rest of this session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact deferred tool name, e.g. 'browse_webpage'."
                    }
                },
                "required": ["name"]
            }
        }
    },
]

# Withheld by default -- see the module docstring above and
# tool/deferred_tools.py's load_tool handler / DEFERRED_TOOLS_RULES text.
DEFERRED_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "capture_screenshot",
            "description": (
                "Captures the primary monitor and saves it as an auto-numbered "
                "screen-N.png inside `directory`. Does NOT show you the image — it only "
                "writes the file. Call view_image on the returned path afterward to "
                "actually see it. Fails cleanly (with a message telling you to skip the "
                "step and continue) in a headless environment with no display."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": (
                            "Where to save the screenshot — pass your session's .jfi/<session> "
                            "folder."
                        )
                    }
                },
                "required": ["directory"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": (
                "Reads an image file (png, jpg, jpeg, gif, or webp) and attaches it to the "
                "conversation so you can see it on your next turn — e.g. a screenshot from "
                "capture_screenshot, or an image already in the project. Unlike read_file, "
                "this shows you the actual picture, not text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the image file."
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage_images",
            "description": (
                "Fetches a web page (http/https only) and downloads the images it references "
                "— the Open Graph/Twitter preview image first, then every <img> tag, in page "
                "order — into `directory` as auto-numbered files (web-1.png, web-2.jpg, ...). "
                "Does NOT show you the image — like capture_screenshot, it only writes files "
                "and reports where. Call view_image on one of the returned paths afterward to "
                "actually see it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The page URL to fetch, e.g. 'https://example.com/article'."
                    },
                    "directory": {
                        "type": "string",
                        "description": (
                            "Where to save downloaded images — pass your session's "
                            ".jfi/<session> folder."
                        )
                    },
                    "max_images": {
                        "type": "integer",
                        "description": "Maximum number of images to download (default 5, capped at 20)."
                    }
                },
                "required": ["url", "directory"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browse_webpage",
            "description": (
                "Loads a URL (http/https only) in a real headless browser with JavaScript "
                "execution — unlike fetch_webpage_images (a plain HTTP GET), this actually "
                "runs the page's JS, so a single-page app or client-rendered content shows up. "
                "Returns the rendered page's title and visible text. Use this only when JS "
                "execution genuinely matters (an SPA, content that renders/updates client-side, "
                "verifying your own app after a real interaction) — a plain static page is "
                "cheaper via curl or fetch_webpage_images. Stateless: one call = one throwaway "
                "browser session (navigate, optionally click once, read, close) — there is no "
                "persistent session across calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The page URL to load, e.g. 'https://example.com/app'."
                    },
                    "click_selector": {
                        "type": "string",
                        "description": (
                            "Optional CSS selector to click once before reading (e.g. "
                            "'button.load-more') — use to open a menu/tab or trigger a "
                            "client-side action, then read what appeared."
                        )
                    },
                    "wait_selector": {
                        "type": "string",
                        "description": (
                            "Optional CSS selector to wait for before reading — use for content "
                            "that renders asynchronously after the initial page load. Ignored "
                            "if click_selector is also given."
                        )
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds allowed for navigation and the optional click/wait (default 15)."
                    },
                    "eval_js": {
                        "type": "string",
                        "description": (
                            "Optional JS EXPRESSION (not a statement) to run in the page after "
                            "the click/wait, e.g. \"document.body.getAttribute('data-theme')\" "
                            "or \"document.querySelectorAll('.item').length\" — its result is "
                            "returned as 'Eval result: ...'. Use this to check DOM state visible "
                            "text can't show (an attribute, a class, a computed style, an "
                            "element count) instead of a custom scripting workaround."
                        )
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "extract_video_frames",
            "description": (
                "Extracts up to max_frames visually distinct frames from a video (the first "
                "frame, plus every point ffmpeg's scene-change detector flags as different "
                "from the previous kept frame) and saves them as auto-numbered frame-N.png "
                "files inside `directory`. Use this to turn a video into a small set of unique "
                "screenshots instead of reviewing it frame-by-frame. Does NOT show you any "
                "image -- it only writes files. Call view_image on the returned paths "
                "afterward to actually see them. Requires the 'ffmpeg' binary; fails cleanly "
                "(with a message telling you to skip the step) if it's not installed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "video_path": {
                        "type": "string",
                        "description": "The relative path to the video file, e.g. 'recordings/demo.mp4'."
                    },
                    "directory": {
                        "type": "string",
                        "description": (
                            "Where to save the extracted frames -- pass your session's "
                            ".jfi/<session> folder."
                        )
                    },
                    "max_frames": {
                        "type": "integer",
                        "description": "Maximum number of distinct frames to extract (default 12, capped at 40)."
                    },
                    "threshold": {
                        "type": "number",
                        "description": (
                            "Scene-change score, 0.05-0.9 (default 0.5 = keep a frame once it's "
                            "at least ~50% different from the last kept frame). Lower catches more/"
                            "subtler changes (more frames); higher only the biggest cuts (fewer "
                            "frames). Retry with a different value if the first result has too "
                            "many near-duplicates or too few frames."
                        )
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds to wait for ffmpeg before giving up (default 300)."
                    }
                },
                "required": ["video_path", "directory"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "start_background_process",
            "description": (
                "Starts a command as a detached background process (a dev/test server you need "
                "running while you do other work) and returns a HANDLE for use with "
                "list_processes/stop_background_process -- never a raw OS pid. Use this instead of "
                "execute_command with a trailing `&`: a process started this way is tracked, so "
                "stopping it later can never accidentally match and kill an unrelated process (the "
                "risk with `pkill`/`kill` by guessed pid or name pattern)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run in the background, e.g. 'uvicorn main:app --port 8000'."
                    },
                    "log_file": {
                        "type": "string",
                        "description": (
                            "Optional path to capture the process's stdout+stderr -- read it back with "
                            "read_file or `tail` once the process has produced output. Omit to discard output."
                        )
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_processes",
            "description": (
                "Lists every process THIS SESSION started with start_background_process (never the "
                "whole OS process table) -- handle, pid, running/exited state, and exit code once "
                "exited. Use this instead of `ps aux`/`ps -ef` to find a background process this "
                "session itself started."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "stop_background_process",
            "description": (
                "Stops a process by the HANDLE start_background_process/list_processes gave you -- "
                "never a raw pid or a name pattern, so this can never match and kill something this "
                "session didn't itself start. Sends SIGTERM to the whole process group first, then "
                "SIGKILL if it hasn't exited within `timeout` seconds. This is the safe replacement "
                "for `pkill`/`kill` by guessed pid or pattern."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "The handle returned by start_background_process, e.g. 'bg1'."
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Seconds to wait for a clean exit before force-killing (default 5)."
                    }
                },
                "required": ["handle"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clear_finished_processes",
            "description": (
                "Prunes every EXITED background process's bookkeeping entry (never a still-"
                "running one) from list_processes/the dashboard's background-processes panel. "
                "Sends no signal -- the OS process is already gone. Use this to tidy up after a "
                "long session accumulates several one-off verification servers you already "
                "confirmed are done with, so the list only shows what's still relevant."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
]

# These three were already defined after the deferred/media tools in this
# file before the CORE/DEFERRED split -- still CORE_TOOLS (always sent),
# just appended here rather than reordering everything above.
CORE_TOOLS += [
    {
        "type": "function",
        "function": {
            "name": "context_save",
            "description": (
                "Saves one fact to your persistent context cache in a single call — merges "
                "it in without disturbing any other key already there. This is the correct "
                "way to add or update a context-cache fact; do NOT read_file + write_file "
                "the whole cache by hand, that risks dropping other keys you didn't retype."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Short identifier for this fact, e.g. 'db_schema'."
                    },
                    "value": {
                        "type": "string",
                        "description": "The fact itself, e.g. 'users table: id, email, created_at'."
                    }
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "context_lookup",
            "description": (
                "Searches your persistent context cache instead of reading the whole file. "
                "Call with no keyword first to list every saved key plus a short preview — "
                "then call again with a keyword (matched against keys and values, case-"
                "insensitive) to get the full text of just what's relevant."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Term to search for. Omit or leave blank to list all saved keys."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_reviewer_note",
            "description": (
                "Leaves a short note for the Reviewer about a problem this implementation step "
                "hit — something you had to work around, an assumption you made because the "
                "plan/spec was ambiguous, a check you couldn't fully verify, a discrepancy from "
                "what was planned. Appends (never overwrites) across however many notes you "
                "leave this phase; the Reviewer reads them all, then they're cleared once that "
                "review pass consumes them. Skip this for clean, uneventful steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The note itself — what happened, and why, concrete enough for the Reviewer to specifically re-check it."
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_reviewer_notes",
            "description": (
                "Shows whatever notes the Implementation agent left via add_reviewer_note during "
                "this pass — problems it hit, workarounds it made, or things it couldn't fully "
                "verify. Empty/no notes just means nothing was flagged. Call this once, near the "
                "start of your review, before deciding PASS/FAIL."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_review_report",
            "description": (
                "Records that the finished work has real problems, triggering another full "
                "planner -> imp -> testing -> reviewer iteration. Only call this when you found "
                "genuine issues after personally re-running the project's own mechanical checks — "
                "a good review needs no call here at all, just reply with a short 'Review: PASS' "
                "summary instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Concrete, actionable issues — one numbered item per problem, naming file(s)/line(s) where relevant, plus how to fix it."
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_plan_feedback",
            "description": (
                "Records that the plan has a real problem, checked against the ACTUAL repo state, "
                "sending it back to the planner for one more pass before you review it again. Only "
                "call this when the plan is genuinely not ready — an approved plan needs no call "
                "here at all, just reply with a short 'Product Owner: APPROVED' summary instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Concrete, actionable feedback — one numbered item per concern, each naming the specific plan item number and/or file involved, plus what should change."
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_plan",
            "description": (
                "Shows the current plan tree — every leaf's id, its display number "
                "(e.g. '1.1.2'), phase, description, and status ('[ ]' todo, '[x]' done). "
                "Parent bullets (anything with children) show no status. Use the leaf id "
                "shown here (NOT the number, which can shift) when calling add_leaf's "
                "parent_id, start_leaf, mark_leaf_done, or split_leaf."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_leaf",
            "description": (
                "Shows ONE leaf's own full detail — description, phase, status, parent, "
                "children, timing — always the complete text, never truncated. Use this to "
                "focus on a single leaf/node instead of re-reading the whole tree via get_plan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "leaf_id": {"type": "integer", "description": "The leaf's id, from get_plan."}
                },
                "required": ["leaf_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_leaf",
            "description": (
                "Adds one new leaf (or root-level parent) to the plan. Replaces hand-editing a "
                "markdown checklist — no numbering to get right, it's computed for display. "
                "A leaf you plan to add children to later must be split_leaf'd once you do; "
                "do not call add_leaf with parent_id pointing at a leaf that already has "
                "status/timing of its own (get_plan shows you which ones do)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phase": {
                        "type": "string",
                        "enum": ["planner", "product_owner", "imp", "testing", "reviewer", "cleanup"],
                        "description": "Which section this belongs under — almost always 'imp' or 'testing'."
                    },
                    "description": {
                        "type": "string",
                        "description": "The smallest doable piece of work this leaf represents."
                    },
                    "parent_id": {
                        "type": "integer",
                        "description": "The parent leaf's id from get_plan. Omit (or 0) for a top-level item."
                    }
                },
                "required": ["phase", "description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "start_leaf",
            "description": (
                "Marks a leaf as the one you're currently working on — records its start "
                "time and shows it as the session's current task. Call this right before "
                "you begin a leaf's real work; call mark_leaf_done when it's finished."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "leaf_id": {"type": "integer", "description": "The leaf's id, from get_plan."}
                },
                "required": ["leaf_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mark_leaf_done",
            "description": (
                "Marks a leaf complete — sets its status to done and records the end time. "
                "Only ever call this on a genuine leaf (no children of its own); marking a "
                "parent bullet done is rejected, same as ticking a markdown checklist's "
                "parent checkbox was never allowed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "leaf_id": {"type": "integer", "description": "The leaf's id, from get_plan."},
                    "tokens": {
                        "type": "integer",
                        "description": "Optional: approximate context cost this leaf took, if worth recording."
                    }
                },
                "required": ["leaf_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "split_leaf",
            "description": (
                "Turns an existing leaf into a parent with new child leaves under it — the "
                "DB-backed replacement for plan_renumber.py's role: no renumbering needed, "
                "since numbers are computed for display, not stored. Use this the moment you "
                "realize a leaf is really more than one piece of work (e.g. it implies writing "
                "a check AND then fixing whatever it finds), rather than attempting it as one "
                "leaf first and only splitting after getting stuck."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "leaf_id": {"type": "integer", "description": "The leaf's id, from get_plan."},
                    "into": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "At least 2 descriptions, one per new child leaf, in the order they should run."
                    }
                },
                "required": ["leaf_id", "into"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reorder_leaf",
            "description": (
                "Moves a leaf to sit right after another leaf among its OWN current "
                "siblings (same parent, same phase) — the DB-backed replacement for "
                "plan_renumber.py's role when you catch an ordering bug (e.g. a leaf verifies "
                "something a LATER-numbered leaf is responsible for creating first). Every "
                "sibling's display number recomputes automatically after the move — nothing "
                "else to fix by hand."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "leaf_id": {"type": "integer", "description": "The leaf's id, from get_plan, to move."},
                    "after_leaf_id": {
                        "type": "integer",
                        "description": "A SIBLING leaf's id to place it right after. Omit (or 0) to move it to the very front."
                    }
                },
                "required": ["leaf_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "merge_leaf",
            "description": (
                "Folds a single child leaf back up into its parent, collapsing pointless "
                "single-child nesting -- the undo for a split_leaf (or Journeyman/Function-"
                "Breakdown pass) that left a parent with only one real child. The parent "
                "absorbs the child's description and becomes a real, actionable leaf itself; "
                "the child is removed. Only works when the parent has EXACTLY this one child "
                "and the child itself has no children of its own -- if a single-child parent "
                "is genuinely the smallest real task, call this instead of fabricating a fake "
                "second child just to satisfy the 'at least 2 children' rule."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "leaf_id": {
                        "type": "integer",
                        "description": "The ONLY CHILD's leaf id (from get_plan) -- it gets folded up into its parent."
                    }
                },
                "required": ["leaf_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_leaf",
            "description": (
                "Removes a genuinely wrong or duplicate leaf outright -- e.g. two byte-"
                "identical leaves created by mistake, or one describing work that turned out "
                "unnecessary. Refuses on a parent (has children -- merge_leaf/delete_leaf "
                "those first) and on a leaf already marked done (that's a real completed-work "
                "record, not a mistake to erase)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "leaf_id": {"type": "integer", "description": "The leaf's id, from get_plan, to remove."}
                },
                "required": ["leaf_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_llm",
            "description": (
                "Asks a fresh, single-turn LLM call anything — write a description, brainstorm "
                "names, clarify an ambiguous requirement, summarize a chunk of text, or handle "
                "any other one-off text task that doesn't need a dedicated tool. This call is "
                "STATELESS: it has NO access to your conversation, the plan, or any files on "
                "disk — put everything it needs directly in the prompt. Do NOT use this for "
                "file operations, running commands, or anything another tool already does "
                "directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The full, self-contained question or instruction to send."
                    }
                },
                "required": ["prompt"]
            }
        }
    },
]

# load_tool is the ONLY tool ever sent by default -- every other tool,
# including the ones used almost every turn (write_file, execute_command,
# read_file, ...), is unlocked on demand. This looks aggressive but the
# economics favor it: unlocking a tool persists for the rest of the session
# (SimpleSessionManager.unlock_tool), so each tool name costs its schema
# exactly ONCE, ever, no matter how many hundreds of turns follow --
# while the per-turn savings from not resending every OTHER tool's schema
# compound for the entire rest of the session. A model that needs several
# tools right away can call load_tool several times in the SAME turn (one
# tool_call per name) to unlock everything it expects to need in one round
# trip instead of paying the bootstrap cost tool-by-tool.
_ALL_TOOLS_BEFORE_SPLIT = CORE_TOOLS + DEFERRED_TOOLS
_load_tool_schema = next(t for t in _ALL_TOOLS_BEFORE_SPLIT if t["function"]["name"] == "load_tool")
CORE_TOOLS = [_load_tool_schema]
DEFERRED_TOOLS = [t for t in _ALL_TOOLS_BEFORE_SPLIT if t is not _load_tool_schema]

# One-line purpose per deferred tool, shown in every system message (see
# DEFERRED_TOOLS_RULES below) so the model knows these exist and roughly
# what each is for WITHOUT paying their full parameter-schema cost until it
# actually calls load_tool(name) to unlock one for real use.
_DEFERRED_TOOL_SUMMARIES = {
    "write_file": "write code/text to a file, creating directories as needed",
    "execute_command": "run a shell command and get its output",
    "read_file": "read an existing file's contents",
    "append_to_file": "append a chunk to a file (build a long document in several smaller calls)",
    "replace_in_file": "replace one exact substring in a file, leaving the rest untouched (tick a checkbox, patch a few lines)",
    "context_save": "save one fact to your persistent context cache (survives history compression)",
    "context_lookup": "search/list your persistent context cache",
    "add_reviewer_note": "(imp) leave a short note for the Reviewer about a problem this step hit",
    "get_reviewer_notes": "(reviewer) read whatever notes the Implementation agent left this pass",
    "write_review_report": "(reviewer) record real problems found, triggering another full iteration",
    "write_plan_feedback": "(product owner) record a real problem with the plan, sending it back to the planner",
    "get_plan": "show the current plan tree (leaf ids, numbers, phase, status)",
    "get_leaf": "show ONE leaf's full detail (description, phase, status, parent, children, timing), always complete, never truncated",
    "add_leaf": "add one new leaf/parent to the plan -- the DB-backed replacement for hand-editing a markdown checklist",
    "start_leaf": "mark a leaf as the one you're currently working on, recording its start time",
    "mark_leaf_done": "mark a leaf complete, recording its end time -- rejected on a parent bullet",
    "split_leaf": "turn a leaf into a parent with new child leaves -- no renumbering needed, unlike plan_renumber.py's old role",
    "reorder_leaf": "move a leaf to sit right after another sibling -- fixes an ordering bug without any manual renumbering",
    "merge_leaf": "fold a single child back into its parent -- the undo for a split_leaf that left a pointless single-child parent",
    "delete_leaf": "remove a genuinely wrong/duplicate leaf outright (refused on a parent or an already-done leaf)",
    "ask_llm": "a fresh, single-turn, STATELESS LLM call for a one-off text task (no file/conversation access)",
    "capture_screenshot": "capture the primary monitor to a PNG (headless environments fail cleanly)",
    "view_image": "attach an image file (a screenshot, a project asset, ...) so you can actually see it next turn",
    "fetch_webpage_images": "plain-HTTP-GET a page and download the images it references (no JS execution)",
    "browse_webpage": "load a URL in a real headless browser with JS execution, optionally click/wait/eval_js, and read the rendered page",
    "extract_video_frames": "pull visually-distinct frames out of a video file as PNGs (needs ffmpeg)",
    "start_background_process": "start a dev/test server (or any long-running command) in the background, tracked by a handle -- not a raw pid",
    "list_processes": "list background processes THIS session started (not the whole OS) -- use instead of `ps`",
    "stop_background_process": "stop a background process by its handle -- use instead of `pkill`/`kill` by guessed pid or pattern",
    "clear_finished_processes": "prune exited processes' bookkeeping entries so list_processes/the dashboard only shows what's still relevant",
}

DEFERRED_TOOL_NAMES = frozenset(_DEFERRED_TOOL_SUMMARIES)
assert DEFERRED_TOOL_NAMES == {t["function"]["name"] for t in DEFERRED_TOOLS}, (
    "_DEFERRED_TOOL_SUMMARIES must list exactly the tools actually in DEFERRED_TOOLS"
)

def deferred_tools_rules(unlocked_tools=()) -> str:
    """The TOOL ACCESS block for the system message -- lists only the
    deferred tools NOT YET unlocked for this session, so it shrinks (and
    disappears entirely once every tool this session ever needs has been
    unlocked) instead of repeating the full 13-tool catalog on every turn
    forever, including turns long after everything's already available.
    """
    remaining = {
        name: summary for name, summary in _DEFERRED_TOOL_SUMMARIES.items()
        if name not in unlocked_tools
    }
    if not remaining:
        return ""
    return (
        "\n    TOOL ACCESS: only load_tool's own schema is available by default -- EVERY\n"
        "    tool listed below is locked until you call load_tool(name) to unlock it.\n"
        "    Unlocking persists for the rest of THIS session (you only ever pay for a given\n"
        "    tool's schema once, no matter how many times you use it after that), so call\n"
        "    load_tool once per name for every tool you expect to need soon (multiple\n"
        "    load_tool calls in the SAME turn are fine and encouraged -- unlock several\n"
        "    together rather than one per turn). Still locked:\n"
        + "\n".join(f"    - {name}: {summary}" for name, summary in remaining.items())
        + "\n"
    )


# Backward-compatible default (nothing unlocked yet) -- the one real caller
# (simple_session_manager.get_system_message) always calls
# deferred_tools_rules(...) with this session's own unlocked_tools() instead.
DEFERRED_TOOLS_RULES = deferred_tools_rules()

# Kept for any caller that wants the full combined list (e.g. offline
# tooling/inspection) -- normal request traffic never sends this whole
# thing at once; see runner._tools_for_session for what actually goes out.
AVAILABLE_TOOLS = CORE_TOOLS + DEFERRED_TOOLS
