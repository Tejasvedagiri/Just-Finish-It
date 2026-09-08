"""Website fetching + image extraction tool.

``fetch_webpage_images`` pulls a URL's HTML and downloads the images it
references onto disk — the same design as ``capture_screenshot`` in
image_tools.py: it returns plain text (a tool result must be plain text on
the wire), never image bytes directly. Call ``view_image`` on one of the
returned paths afterward to actually see a picture.

No new dependency: ``httpx`` is already a project dependency (pyproject.toml).
Image extraction is a couple of small regexes rather than a full HTML parser
(no BeautifulSoup/lxml dependency) — good enough for real-world pages, and
consistent with this project's minimal-dependency approach elsewhere.
"""

import re
from pathlib import Path
from typing import List
from urllib.parse import urljoin, urlparse

import httpx

from JFI.tool.file_tools import _get_safe_path
from JFI.tool.image_tools import MAX_IMAGE_BYTES, _MIME_BY_EXT

FETCH_TIMEOUT_SECONDS = 15.0
# Bounds how much of the response body gets regex-scanned for <img> tags —
# an unbounded HTML page (or a server that mislabels a huge non-HTML
# response as text/html) shouldn't turn into an unbounded regex scan.
MAX_PAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_IMAGES = 5
# Hard ceiling regardless of what the model asks for — this tool downloads
# from an arbitrary remote server, so a runaway `max_images` argument (typo'd
# or otherwise) must not turn into dozens of outbound requests.
MAX_IMAGES_CAP = 20

_USER_AGENT = "Mozilla/5.0 (compatible; JFI-agent/1.0)"

# Extension guessed from the response's Content-Type — reversed from
# image_tools._MIME_BY_EXT so a download only ever lands under an extension
# view_image already knows how to open. Built in ascending key order so "jpg"
# (sorts after "jpeg") is the one processed last and wins the image/jpeg
# collision — the more common convention for a freshly-downloaded file.
_EXT_BY_MIME = {mime: ext for ext, mime in sorted(_MIME_BY_EXT.items())}

_IMG_TAG_RE = re.compile(r'<img\b[^>]*?\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
_OG_IMAGE_RE = re.compile(
    r'<meta\b[^>]*?\bproperty=["\']og:image["\'][^>]*?\bcontent=["\']([^"\']+)["\']', re.IGNORECASE
)
_TWITTER_IMAGE_RE = re.compile(
    r'<meta\b[^>]*?\bname=["\']twitter:image["\'][^>]*?\bcontent=["\']([^"\']+)["\']', re.IGNORECASE
)


def _extract_image_urls(html: str, base_url: str) -> List[str]:
    """Open Graph / Twitter preview images first (usually the page's single
    most representative image), then every <img src>, in document order,
    de-duplicated and resolved to absolute URLs against `base_url`."""
    found: List[str] = []
    for pattern in (_OG_IMAGE_RE, _TWITTER_IMAGE_RE, _IMG_TAG_RE):
        for match in pattern.findall(html):
            absolute = urljoin(base_url, match.strip())
            if absolute not in found:
                found.append(absolute)
    return found


def fetch_webpage_images(url: str, directory: str, max_images: int = DEFAULT_MAX_IMAGES) -> str:
    """
    Fetches `url`, extracts the images it references (Open Graph/Twitter
    preview image first, then every <img> tag, in page order), and downloads
    up to `max_images` of them into `directory` as auto-numbered files
    (web-1.png, web-2.jpg, ...).

    Does NOT show you any image — like capture_screenshot, it only writes
    files to disk and reports where. Call view_image on one of the returned
    paths afterward to actually see it.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return (
            f"Error: unsupported URL scheme '{parsed.scheme or '(none)'}'. Only http/https "
            "URLs are supported."
        )

    try:
        out_dir = _get_safe_path(directory)
    except PermissionError as e:
        return f"Error: {e}"
    out_dir.mkdir(parents=True, exist_ok=True)

    max_images = max(1, min(int(max_images), MAX_IMAGES_CAP))

    try:
        with httpx.Client(follow_redirects=True, timeout=FETCH_TIMEOUT_SECONDS,
                           headers={"User-Agent": _USER_AGENT}) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return f"Error: {url} returned HTTP {e.response.status_code}."
    except httpx.HTTPError as e:
        return f"Error fetching {url}: {e}"

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        return (
            f"Error: {url} returned content-type '{content_type or 'unknown'}', not HTML — "
            "nothing to parse for images. If this URL IS an image, download it a different "
            "way (e.g. execute_command with curl) then use view_image."
        )

    html = response.text[:MAX_PAGE_BYTES]
    image_urls = _extract_image_urls(html, str(response.url))
    if not image_urls:
        return f"No images found on {url}."

    downloaded: List[tuple] = []
    errors: List[str] = []
    existing = [
        int(m.group(1)) for p in out_dir.glob("web-*.*")
        if (m := re.match(r"^web-(\d+)\.\w+$", p.name))
    ]
    next_index = max(existing, default=0) + 1

    with httpx.Client(follow_redirects=True, timeout=FETCH_TIMEOUT_SECONDS,
                       headers={"User-Agent": _USER_AGENT}) as client:
        for image_url in image_urls:
            if len(downloaded) >= max_images:
                break
            try:
                img_response = client.get(image_url)
                img_response.raise_for_status()
            except httpx.HTTPError as e:
                errors.append(f"{image_url}: {e}")
                continue

            img_content_type = img_response.headers.get("content-type", "").split(";")[0].strip().lower()
            ext = _EXT_BY_MIME.get(img_content_type)
            if ext is None:
                # Server didn't send a recognized image content-type — fall
                # back to the URL's own extension if it's one view_image
                # supports, otherwise skip it rather than guess.
                suffix = Path(urlparse(image_url).path).suffix.lstrip(".").lower()
                ext = suffix if suffix in _MIME_BY_EXT else None
            if ext is None:
                errors.append(f"{image_url}: unrecognized image type ({img_content_type or 'unknown'})")
                continue

            data = img_response.content
            if len(data) > MAX_IMAGE_BYTES:
                errors.append(f"{image_url}: {len(data)} bytes, over the {MAX_IMAGE_BYTES}-byte limit")
                continue

            out_path = out_dir / f"web-{next_index}.{ext}"
            next_index += 1
            out_path.write_bytes(data)
            downloaded.append((image_url, out_path))

    if not downloaded:
        detail = f" ({'; '.join(errors[:3])})" if errors else ""
        return f"Error: found {len(image_urls)} image URL(s) on {url} but none could be downloaded{detail}."

    lines = [f"Success: downloaded {len(downloaded)} image(s) from {url}:"]
    for image_url, out_path in downloaded:
        lines.append(f"  {out_path}  <- {image_url}")
    if errors:
        lines.append(f"({len(errors)} other image(s) skipped — malformed URL or fetch error)")
    lines.append("Call view_image on one of the paths above to actually see it.")
    return "\n".join(lines)
