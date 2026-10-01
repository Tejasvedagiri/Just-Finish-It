"""JSON schemas of the shared tools (files, shell, processes, browser and
media, context cache, reviewer notes, plan reads, ask_llm). Episodes pick
from TOOL_SCHEMAS by name (JFI.episode.tools): each role gets its core set,
and the rarely-needed ones (roles.OPTIONAL_POOL) through that episode's own
load_tool. The planner's node tools, the code tools and the runbook/design
tools keep their schemas next to their implementations."""

TOOL_SCHEMAS = [
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
                "Replaces one exact substring in a file, leaving the rest untouched: the way to "
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
]

TOOL_SCHEMAS += [
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
                            "Where to save the screenshot, e.g. the project's .jfi folder."
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
                            "Where to save downloaded images, e.g. the project's .jfi folder."
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
            "name": "check_page",
            "description": (
                "A web page's end-to-end check: loads the URL in a headless browser, waits for the network to "
                "settle, and reports console errors, uncaught exceptions and failed or 4xx/5xx requests, plus "
                "the visible text and a screenshot path. Start the app first (the runbook's run command, as a "
                "background process), then check the runbook's view URL; a static page with no server can be "
                "checked as a file:/// URL."
            ),
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "e.g. the runbook's view URL, http://localhost:5173"},
                "timeout": {"type": "integer", "description": "seconds to wait for the page (default 15)"},
            }, "required": ["url"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "Call an HTTP API the project serves and get the status, key headers and the body (JSON "
                "pretty-printed). Use it instead of curl through execute_command."
            ),
            "parameters": {"type": "object", "properties": {
                "method": {"type": "string", "description": "GET, POST, PUT, PATCH or DELETE"},
                "url": {"type": "string"},
                "body": {"type": "string", "description": "request body; JSON is sent as application/json"},
                "headers": {"type": "object", "description": "extra request headers"},
            }, "required": ["method", "url"]},
        },
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
                            "Where to save the extracted frames, e.g. the project's .jfi folder."
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

TOOL_SCHEMAS += [
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
                "Records work the plan never covered, which starts another planner -> imp -> "
                "reviewer iteration to add it. A bug in code that was built goes to reopen_leaf "
                "instead, and a pass is finish (which re-runs the e2e itself). Only call this for "
                "genuine gaps you confirmed by running the project's own checks."
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
