"""PodScripts podcast listing page crawler for personal transcript export."""

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen


USER_AGENT = "dictation-pdf-maker/1.0 (+https://github.com)"
EPISODE_LINK_PATTERN = re.compile(
    r'<h3>\s*<a href="(?P<href>/podcasts/[^"]+/[^"/?#]+)">(?P<title>.*?)</a>\s*</h3>',
    re.IGNORECASE | re.DOTALL,
)
H1_PATTERN = re.compile(r"<h1[^>]*>(?P<text>.*?)</h1>", re.IGNORECASE | re.DOTALL)
DESCRIPTION_PATTERN = re.compile(r"<h3[^>]*>(?P<text>.*?)</h3>", re.IGNORECASE | re.DOTALL)
DATE_PATTERN = re.compile(
    r'<span[^>]*class="episode_date"[^>]*>\s*Episode Date:\s*(?P<date>.*?)\s*</span>',
    re.IGNORECASE | re.DOTALL,
)
TIMESTAMP_PATTERN = re.compile(
    r'<span[^>]*class="pod_timestamp_indicator"[^>]*>(?P<text>.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
TRANSCRIPT_TEXT_PATTERN = re.compile(
    r'<span[^>]*class="[^"]*\btranscript-text\b[^"]*"[^>]*>(?P<text>.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
WHITESPACE_PATTERN = re.compile(r"\s+")


class HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def get_text(self) -> str:
        return "".join(self.parts)


@dataclass(frozen=True)
class EpisodeLink:
    title: str
    url: str


@dataclass(frozen=True)
class EpisodeTranscript:
    podcast_title: str
    episode_title: str
    episode_date: str | None
    description: str | None
    url: str
    transcript: str
    output_file: str


def normalize_spaces(text: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def strip_html(text: str) -> str:
    parser = HTMLTextExtractor()
    parser.feed(unescape(text))
    parser.close()
    return normalize_spaces(parser.get_text())


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", value)
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    return cleaned or "transcript"


def fetch_html(url: str, timeout: float) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def validate_podcast_page_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL must start with http:// or https://")
    if parsed.netloc not in {"podscripts.co", "www.podscripts.co"}:
        raise ValueError("Only podscripts.co URLs are supported")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "podcasts":
        raise ValueError("Use a podcast listing URL like https://podscripts.co/podcasts/<slug>?page=2")


def extract_page_number(url: str) -> int:
    query = parse_qs(urlparse(url).query)
    try:
        return int(query.get("page", ["1"])[0])
    except ValueError:
        return 1


def extract_episode_links(listing_html: str, base_url: str) -> list[EpisodeLink]:
    episode_links: list[EpisodeLink] = []
    seen_urls: set[str] = set()

    for match in EPISODE_LINK_PATTERN.finditer(listing_html):
        absolute_url = urljoin(base_url, unescape(match.group("href")))
        if absolute_url in seen_urls:
            continue

        seen_urls.add(absolute_url)
        episode_links.append(
            EpisodeLink(
                title=strip_html(match.group("title")),
                url=absolute_url,
            )
        )

    return episode_links


def extract_balanced_div(html: str, marker: str) -> str:
    marker_index = html.find(marker)
    if marker_index == -1:
        raise ValueError(f"Could not find marker: {marker}")

    start_index = html.rfind("<div", 0, marker_index)
    if start_index == -1:
        raise ValueError(f"Could not locate opening <div> for marker: {marker}")

    div_tag_pattern = re.compile(r"</?div\b", re.IGNORECASE)
    depth = 0
    for match in div_tag_pattern.finditer(html, start_index):
        tag_start = match.start()
        tag_end = html.find(">", tag_start)
        if tag_end == -1:
            break

        is_closing = html[tag_start + 1] == "/"
        if is_closing:
            depth -= 1
            if depth == 0:
                return html[start_index : tag_end + 1]
        else:
            depth += 1

    raise ValueError(f"Could not extract balanced <div> for marker: {marker}")


def extract_episode_metadata(html: str, fallback_title: str) -> tuple[str, str | None, str | None]:
    header_match = H1_PATTERN.search(html)
    header_text = strip_html(header_match.group("text")) if header_match else fallback_title

    podcast_title = header_text
    episode_title = fallback_title
    if " - " in header_text:
        podcast_title, episode_title = [part.strip() for part in header_text.split(" - ", 1)]

    date_match = DATE_PATTERN.search(html)
    episode_date = strip_html(date_match.group("date")) if date_match else None

    description_match = DESCRIPTION_PATTERN.search(html)
    description = strip_html(description_match.group("text")) if description_match else None

    return podcast_title, episode_date, description or None


def extract_transcript_text(html: str) -> str:
    transcript_html = extract_balanced_div(html, 'class="podcast-transcript"')
    sentence_blocks = re.findall(
        r'<div[^>]*class="single-sentence"[^>]*>(.*?)</div>',
        transcript_html,
        re.IGNORECASE | re.DOTALL,
    )
    if not sentence_blocks:
        raise ValueError("Could not find transcript sentences")

    transcript_parts: list[str] = []
    for block in sentence_blocks:
        timestamp_match = TIMESTAMP_PATTERN.search(block)
        timestamp = strip_html(timestamp_match.group("text")) if timestamp_match else None

        fragments = [
            strip_html(fragment_match.group("text"))
            for fragment_match in TRANSCRIPT_TEXT_PATTERN.finditer(block)
        ]
        fragments = [fragment for fragment in fragments if fragment]
        if not fragments:
            continue

        section_lines = []
        if timestamp:
            section_lines.append(timestamp)
        section_lines.append(" ".join(fragments))
        transcript_parts.append("\n".join(section_lines))

    transcript_text = "\n\n".join(part for part in transcript_parts if part.strip())
    if not transcript_text:
        raise ValueError("Transcript was empty after parsing")

    return transcript_text


def build_output_directory(base_output_dir: Path, page_url: str) -> Path:
    parts = [part for part in urlparse(page_url).path.split("/") if part]
    podcast_slug = parts[-1]
    page_number = extract_page_number(page_url)
    return base_output_dir / f"{podcast_slug}_page_{page_number}"


def iter_episode_transcripts(
    episode_links: Iterable[EpisodeLink],
    output_dir: Path,
    delay_seconds: float,
    timeout: float,
    limit: int | None,
) -> list[EpisodeTranscript]:
    transcripts: list[EpisodeTranscript] = []
    used_filenames: set[str] = set()

    for index, episode_link in enumerate(episode_links, start=1):
        if limit is not None and index > limit:
            break

        if index > 1 and delay_seconds > 0:
            time.sleep(delay_seconds)

        episode_html = fetch_html(episode_link.url, timeout=timeout)
        podcast_title, episode_date, description = extract_episode_metadata(
            episode_html,
            fallback_title=episode_link.title,
        )
        transcript_text = extract_transcript_text(episode_html)

        filename = sanitize_filename(episode_link.title)
        unique_filename = filename
        suffix = 2
        while f"{unique_filename}.txt" in used_filenames:
            unique_filename = f"{filename}_{suffix}"
            suffix += 1
        unique_filename = f"{unique_filename}.txt"
        used_filenames.add(unique_filename)

        output_path = output_dir / unique_filename
        output_body = [
            f"Podcast: {podcast_title}",
            f"Episode: {episode_link.title}",
        ]
        if episode_date:
            output_body.append(f"Episode Date: {episode_date}")
        output_body.append(f"Source URL: {episode_link.url}")
        output_body.append("")
        output_body.append(transcript_text)
        output_path.write_text("\n".join(output_body), encoding="utf-8")

        transcripts.append(
            EpisodeTranscript(
                podcast_title=podcast_title,
                episode_title=episode_link.title,
                episode_date=episode_date,
                description=description,
                url=episode_link.url,
                transcript=transcript_text,
                output_file=str(output_path),
            )
        )

    return transcripts


def write_metadata_file(output_dir: Path, page_url: str, episodes: list[EpisodeTranscript]) -> Path:
    metadata = {
        "source_page_url": page_url,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "episode_count": len(episodes),
        "episodes": [
            {
                **asdict(episode),
                "transcript": None,
            }
            for episode in episodes
        ],
    }
    metadata_path = output_dir / "metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metadata_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl all episode transcripts linked from a single PodScripts podcast listing page.",
    )
    parser.add_argument(
        "page_url",
        help="PodScripts podcast page URL, for example https://podscripts.co/podcasts/conan-obrien-needs-a-friend?page=2",
    )
    parser.add_argument(
        "--output-dir",
        default="downloads",
        help="Base directory where crawled transcripts will be written. Default: downloads",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=0.5,
        help="Delay between episode requests. Default: 0.5",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds. Default: 20",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for how many episode pages to crawl from the listing page.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        validate_podcast_page_url(args.page_url)
        listing_html = fetch_html(args.page_url, timeout=args.timeout)
        episode_links = extract_episode_links(listing_html, base_url=args.page_url)
        if not episode_links:
            raise ValueError("No episode links were found on the page")

        output_dir = build_output_directory(Path(args.output_dir), args.page_url)
        output_dir.mkdir(parents=True, exist_ok=True)

        transcripts = iter_episode_transcripts(
            episode_links=episode_links,
            output_dir=output_dir,
            delay_seconds=max(args.delay_seconds, 0.0),
            timeout=args.timeout,
            limit=args.limit,
        )
        if not transcripts:
            raise ValueError("No transcripts were written")

        metadata_path = write_metadata_file(output_dir, args.page_url, transcripts)
    except (ValueError, HTTPError, URLError) as error:
        print(f"Error: {error}")
        return 1

    print(f"Saved {len(transcripts)} transcript files to {output_dir}")
    print(f"Metadata file: {metadata_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
