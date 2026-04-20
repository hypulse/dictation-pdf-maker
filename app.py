import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer


SRT_TIMECODE_PATTERN = re.compile(
    r"^\s*\d{2}:\d{2}:\d{2}[,.:]\d{1,3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.:]\d{1,3}"
)
SENTENCE_PATTERN = re.compile(r".+?(?:[.!?。！？]+[\"'”’)]*|$)")
SENTENCE_END_PUNCT_PATTERN = re.compile(r'([.!?。！？]+["\'”’)]*)$')
PODSCRIPTS_TIMESTAMP_PATTERN = re.compile(
    r"^\s*Starting point is\s+(\d{2}:\d{2}:\d{2})\s*$",
    re.IGNORECASE,
)

WORD_MODE = "단어별 빈칸"
SENTENCE_MODE = "문장 전체 빈칸"
FILE_INPUT_MODE = "파일 업로드"
DIRECT_INPUT_MODE = "텍스트 붙여넣기"
PLAIN_TEXT_FORMAT = "일반 텍스트"
SRT_FORMAT = "SRT 자막"
TEXT_PATTERN_BASIC = "일반 텍스트"
TEXT_PATTERN_PODSCRIPTS = "PodScripts Transcript"
PDF_FONT_NAME = "HYGothic-Medium"
DISPLAY_TIMESTAMP_PATTERN = re.compile(r"(\d{2}:\d{2}:\d{2})")
MEANINGFUL_CHAR_PATTERN = re.compile(r"[^\W_]", re.UNICODE)
WORD_LINE_PIXEL_PER_CHAR = 6
MIN_WORD_LINE_PIXEL_WIDTH = 18
PIXEL_TO_POINT = 72 / 96
WORD_LINE_POINT_PER_CHAR = WORD_LINE_PIXEL_PER_CHAR * PIXEL_TO_POINT
MIN_WORD_LINE_POINT_WIDTH = MIN_WORD_LINE_PIXEL_WIDTH * PIXEL_TO_POINT
WORD_LINE_STROKE_WIDTH = 1


@dataclass(frozen=True)
class TextUnit:
    text: str
    timestamp: str | None = None


@dataclass(frozen=True)
class RenderSegment:
    kind: str
    width: float
    text: str = ""
    font_name: str = PDF_FONT_NAME
    font_size: float = 13
    color: colors.Color = colors.black


@dataclass(frozen=True)
class PreparedSource:
    source_name: str
    units: list[TextUnit]


def decode_text(raw_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build_direct_input_name(title: str, source_format: str) -> str:
    normalized_title = normalize_spaces(title) or "dictation"
    extension = ".srt" if source_format == SRT_FORMAT else ".txt"
    return f"{normalized_title}{extension}"


def sanitize_filename_component(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", normalize_spaces(name))
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "dictation"


def split_sentences(text: str) -> list[str]:
    cleaned = normalize_spaces(text)
    if not cleaned:
        return []

    parts = [match.group(0).strip() for match in SENTENCE_PATTERN.finditer(cleaned)]
    return [part for part in parts if part]


def join_content_lines(lines: list[str]) -> str:
    return normalize_spaces(" ".join(line.strip() for line in lines if line.strip()))


def is_srt_file(filename: str) -> bool:
    return Path(filename).suffix.lower() == ".srt"


def format_display_timestamp(timestamp: str | None) -> str | None:
    if not timestamp:
        return None

    match = DISPLAY_TIMESTAMP_PATTERN.search(timestamp)
    if match:
        return match.group(1)
    return timestamp


def extract_srt_units(text: str) -> list[TextUnit]:
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    units: list[TextUnit] = []

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        timestamp = None
        content_lines = []
        for line in lines:
            if re.fullmatch(r"\d+", line):
                continue
            if SRT_TIMECODE_PATTERN.match(line):
                timestamp = format_display_timestamp(normalize_spaces(line))
                continue
            content_lines.append(line)

        content = join_content_lines(content_lines)
        if not content:
            continue

        units.append(TextUnit(text=content, timestamp=timestamp))

    return units


def extract_podscripts_units(normalized_text: str) -> list[TextUnit]:
    lines = normalized_text.splitlines()
    units: list[TextUnit] = []
    current_timestamp: str | None = None
    current_lines: list[str] = []
    saw_timestamp = False

    def flush_current() -> None:
        nonlocal current_timestamp, current_lines
        content = join_content_lines(current_lines)
        if content:
            units.append(TextUnit(text=content, timestamp=current_timestamp))
        current_timestamp = None
        current_lines = []

    for raw_line in lines:
        line = raw_line.strip()
        match = PODSCRIPTS_TIMESTAMP_PATTERN.match(line)
        if match:
            saw_timestamp = True
            flush_current()
            current_timestamp = match.group(1)
            continue

        if line:
            current_lines.append(line)
            continue

        flush_current()

    flush_current()

    if saw_timestamp:
        return units
    return []


def extract_txt_units(text: str, text_pattern: str = TEXT_PATTERN_BASIC) -> list[TextUnit]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if text_pattern == TEXT_PATTERN_PODSCRIPTS:
        podscripts_units = extract_podscripts_units(normalized)
        if podscripts_units:
            return podscripts_units

    if re.search(r"\n\s*\n", normalized):
        blocks = re.split(r"\n\s*\n", normalized)
        return [
            TextUnit(text=content)
            for content in (join_content_lines(block.splitlines()) for block in blocks)
            if content
        ]

    return [
        TextUnit(text=content)
        for content in (join_content_lines([line]) for line in normalized.splitlines())
        if content
    ]


def extract_units(
    filename: str,
    raw_text: str,
    text_pattern: str = TEXT_PATTERN_BASIC,
) -> list[TextUnit]:
    if is_srt_file(filename):
        return extract_srt_units(raw_text)
    return extract_txt_units(raw_text, text_pattern=text_pattern)


def normalize_meaningful_token(token: str) -> str:
    candidate = re.sub(r"^[^\w]+|[^\w]+$", "", token)
    if candidate and MEANINGFUL_CHAR_PATTERN.search(candidate):
        return candidate
    return ""


def extract_meaningful_words(sentence: str) -> list[str]:
    words: list[str] = []
    for token in sentence.split():
        normalized = normalize_meaningful_token(token)
        if normalized:
            words.append(normalized)
    return words


def split_first_meaningful_word(sentence: str) -> tuple[str, str]:
    tokens = sentence.split()
    if not tokens:
        return "", ""

    for index, token in enumerate(tokens):
        normalized = normalize_meaningful_token(token)
        if normalized:
            remaining = " ".join(tokens[index + 1 :]).strip()
            return normalized, remaining

    return "", ""


def split_sentence_body_and_punct(sentence: str) -> tuple[str, str]:
    cleaned = normalize_spaces(sentence)
    if not cleaned:
        return "", ""

    match = SENTENCE_END_PUNCT_PATTERN.search(cleaned)
    if not match:
        return cleaned, ""

    punct = match.group(1)
    body = cleaned[: -len(punct)].rstrip()
    return body, punct


def mask_word_mode(sentence: str, show_first_word: bool) -> str:
    words = extract_meaningful_words(sentence)
    if not words:
        return ""

    masked_words = []
    for index, word in enumerate(words):
        if show_first_word and index == 0:
            masked_words.append(word)
        else:
            masked_words.append("_" * len(word))

    return " ".join(masked_words)


def mask_sentence_mode(sentence: str, show_first_word: bool) -> str:
    sentence = sentence.strip()
    total_length = len(sentence)
    if total_length == 0:
        return ""

    if show_first_word:
        first_word, rest = split_first_meaningful_word(sentence)
        if not first_word:
            return "_" * total_length
        remaining_length = len(rest)
        if remaining_length == 0:
            return first_word
        return f"{first_word} {'_' * remaining_length}"

    return "_" * total_length


def transform_sentence(sentence: str, mode: str, show_first_word: bool) -> str:
    body, punct = split_sentence_body_and_punct(sentence)
    if mode == WORD_MODE:
        transformed = mask_word_mode(body, show_first_word)
    else:
        transformed = mask_sentence_mode(body, show_first_word)
    return f"{transformed}{punct}"


def transform_text_block(text: str, mode: str, show_first_word: bool) -> str:
    sentences = split_sentences(text)
    if not sentences:
        return transform_sentence(text, mode=mode, show_first_word=show_first_word)
    return " ".join(
        transform_sentence(sentence, mode=mode, show_first_word=show_first_word)
        for sentence in sentences
    )


def transform_unit(unit: TextUnit, mode: str, show_first_word: bool) -> TextUnit:
    return TextUnit(
        text=transform_text_block(unit.text, mode=mode, show_first_word=show_first_word),
        timestamp=unit.timestamp,
    )


def format_unit_for_preview(unit: TextUnit, show_timestamps: bool) -> str:
    if show_timestamps and unit.timestamp:
        return f"{unit.timestamp}  {unit.text}"
    return unit.text


def build_preview_text(transformed_units: Iterable[TextUnit], show_timestamps: bool) -> str:
    preview_units = [
        format_unit_for_preview(unit, show_timestamps)
        for unit in transformed_units
        if unit.text
    ]
    return "\n\n".join(preview_units)


def make_text_segment(
    text: str,
    font_name: str,
    font_size: float,
    color: colors.Color = colors.black,
) -> RenderSegment:
    return RenderSegment(
        kind="text",
        text=text,
        width=pdfmetrics.stringWidth(text, font_name, font_size),
        font_name=font_name,
        font_size=font_size,
        color=color,
    )


def make_space_segment(width: float) -> RenderSegment:
    return RenderSegment(kind="space", width=width)


def make_line_segment(char_count: int) -> RenderSegment:
    line_width = max(char_count * WORD_LINE_POINT_PER_CHAR, MIN_WORD_LINE_POINT_WIDTH)
    return RenderSegment(kind="line", width=line_width)


def append_timestamp_segments(
    segments: list[RenderSegment],
    unit: TextUnit,
    body_style: ParagraphStyle,
    show_timestamps: bool,
) -> None:
    if not (show_timestamps and unit.timestamp):
        return

    timestamp_gap = pdfmetrics.stringWidth("  ", body_style.fontName, body_style.fontSize)
    segments.append(
        make_text_segment(
            unit.timestamp,
            font_name=PDF_FONT_NAME,
            font_size=8,
            color=colors.HexColor("#666666"),
        )
    )
    segments.append(make_space_segment(timestamp_gap))


def append_sentence_gap(
    segments: list[RenderSegment],
    body_style: ParagraphStyle,
    sentence_index: int,
    total_sentences: int,
) -> None:
    if sentence_index >= total_sentences - 1:
        return

    sentence_gap = pdfmetrics.stringWidth(" ", body_style.fontName, body_style.fontSize)
    segments.append(make_space_segment(sentence_gap))


def build_word_mode_segments(
    unit: TextUnit,
    show_first_word: bool,
    body_style: ParagraphStyle,
    show_timestamps: bool,
) -> list[RenderSegment]:
    sentence_parts = [split_sentence_body_and_punct(sentence) for sentence in split_sentences(unit.text)]
    if not sentence_parts:
        return []

    segments: list[RenderSegment] = []
    space_width = pdfmetrics.stringWidth(" ", body_style.fontName, body_style.fontSize)
    append_timestamp_segments(segments, unit, body_style, show_timestamps)

    for sentence_index, (body, punct) in enumerate(sentence_parts):
        words = extract_meaningful_words(body)
        for word_index, word in enumerate(words):
            if segments:
                previous = segments[-1]
                if previous.kind != "space":
                    segments.append(make_space_segment(space_width))

            if show_first_word and word_index == 0:
                segments.append(
                    make_text_segment(
                        word,
                        font_name=body_style.fontName,
                        font_size=body_style.fontSize,
                        color=colors.black,
                    )
                )
            else:
                segments.append(make_line_segment(len(word)))

        if punct:
            segments.append(
                make_text_segment(
                    punct,
                    font_name=body_style.fontName,
                    font_size=body_style.fontSize,
                    color=colors.black,
                )
            )

        append_sentence_gap(segments, body_style, sentence_index, len(sentence_parts))

    return segments


def build_sentence_mode_segments(
    unit: TextUnit,
    show_first_word: bool,
    body_style: ParagraphStyle,
    show_timestamps: bool,
) -> list[RenderSegment]:
    sentence_parts = [split_sentence_body_and_punct(sentence) for sentence in split_sentences(unit.text)]
    if not sentence_parts:
        return []

    segments: list[RenderSegment] = []
    append_timestamp_segments(segments, unit, body_style, show_timestamps)

    for sentence_index, (body, punct) in enumerate(sentence_parts):
        total_length = len(body)
        if total_length > 0:
            if show_first_word:
                first_word, rest = split_first_meaningful_word(body)
                if first_word:
                    segments.append(
                        make_text_segment(
                            first_word,
                            font_name=body_style.fontName,
                            font_size=body_style.fontSize,
                            color=colors.black,
                        )
                    )

                    remaining_length = len(rest)
                    if remaining_length > 0:
                        space_width = pdfmetrics.stringWidth(" ", body_style.fontName, body_style.fontSize)
                        segments.append(make_space_segment(space_width))
                        segments.append(make_line_segment(remaining_length))
                else:
                    segments.append(make_line_segment(total_length))
            else:
                segments.append(make_line_segment(total_length))

        if punct:
            segments.append(
                make_text_segment(
                    punct,
                    font_name=body_style.fontName,
                    font_size=body_style.fontSize,
                    color=colors.black,
                )
            )

        append_sentence_gap(segments, body_style, sentence_index, len(sentence_parts))

    return segments


def split_line_segment(width: float, avail_width: float, current_width: float) -> list[float]:
    pieces: list[float] = []
    remaining_width = width
    current_line_width = current_width

    while remaining_width > 0:
        available_width = avail_width - current_line_width
        if available_width <= 0:
            current_line_width = 0.0
            continue

        piece_width = min(remaining_width, available_width)
        pieces.append(piece_width)
        remaining_width -= piece_width
        current_line_width += piece_width
        if remaining_width > 0:
            current_line_width = 0.0

    return pieces


def layout_segments(segments: list[RenderSegment], avail_width: float) -> list[list[RenderSegment]]:
    if not segments:
        return []

    lines: list[list[RenderSegment]] = []
    current_line: list[RenderSegment] = []
    current_width = 0.0

    for segment in segments:
        if segment.kind == "space" and not current_line:
            continue

        if segment.kind == "line":
            line_pieces = split_line_segment(segment.width, avail_width, current_width)
            for piece_index, piece_width in enumerate(line_pieces):
                if piece_index > 0 and current_line:
                    lines.append(current_line)
                    current_line = []
                    current_width = 0.0

                current_line.append(RenderSegment(kind="line", width=piece_width))
                current_width += piece_width

                if current_width >= avail_width and piece_index < len(line_pieces) - 1:
                    lines.append(current_line)
                    current_line = []
                    current_width = 0.0
            continue

        if current_line and current_width + segment.width > avail_width:
            lines.append(current_line)
            current_line = []
            current_width = 0.0
            if segment.kind == "space":
                continue

        if segment.kind == "space" and current_line and current_width + segment.width > avail_width:
            continue

        current_line.append(segment)
        current_width += segment.width

    if current_line:
        lines.append(current_line)

    return lines


class WordMaskFlowable(Flowable):
    def __init__(
        self,
        segments: list[RenderSegment],
        body_style: ParagraphStyle,
    ) -> None:
        super().__init__()
        self.segments = segments
        self.body_style = body_style
        self.lines: list[list[RenderSegment]] = []
        self.width = 0.0
        self.height = 0.0

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        self.lines = layout_segments(self.segments, availWidth)
        if not self.lines:
            self.width = availWidth
            self.height = 0.0
            return availWidth, 0.0

        self.width = availWidth
        self.height = (len(self.lines) * self.body_style.leading) + self.body_style.spaceAfter
        return availWidth, self.height

    def draw(self) -> None:
        canvas = self.canv
        baseline_adjust = self.body_style.fontSize * 0.15

        for line_index, line in enumerate(self.lines):
            x = 0.0
            baseline_y = self.height - ((line_index + 1) * self.body_style.leading) + baseline_adjust

            for segment in line:
                if segment.kind == "text":
                    canvas.setFillColor(segment.color)
                    canvas.setFont(segment.font_name, segment.font_size)
                    canvas.drawString(x, baseline_y, segment.text)
                elif segment.kind == "line":
                    canvas.setStrokeColor(colors.black)
                    canvas.setLineWidth(WORD_LINE_STROKE_WIDTH)
                    line_y = baseline_y - 1
                    canvas.line(x, line_y, x + segment.width, line_y)

                x += segment.width


def build_answer_key_paragraph(
    unit: TextUnit,
    show_timestamps: bool,
    body_style: ParagraphStyle,
) -> Paragraph:
    if show_timestamps and unit.timestamp:
        markup = (
            f'<font name="{PDF_FONT_NAME}" size="8" color="#666666">{escape(unit.timestamp)}</font> '
            f"{escape(unit.text)}"
        )
        return Paragraph(markup, body_style)
    return Paragraph(escape(unit.text), body_style)


def register_pdf_font() -> None:
    if PDF_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT_NAME))


def build_pdf(
    units: Iterable[TextUnit],
    original_name: str,
    mode: str,
    show_first_word: bool,
    show_timestamps: bool,
) -> bytes:
    register_pdf_font()
    source_units = [unit for unit in units if unit.text]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"{Path(original_name).stem} dictation worksheet",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "WorksheetTitle",
        parent=styles["Title"],
        fontName=PDF_FONT_NAME,
        fontSize=16,
        leading=20,
        spaceAfter=6,
    )
    meta_style = ParagraphStyle(
        "WorksheetMeta",
        parent=styles["Normal"],
        fontName=PDF_FONT_NAME,
        fontSize=9,
        leading=12,
        spaceAfter=2,
        textColor=colors.HexColor("#666666"),
    )
    body_style = ParagraphStyle(
        "WorksheetBody",
        parent=styles["Normal"],
        fontName=PDF_FONT_NAME,
        fontSize=13,
        leading=22,
        spaceAfter=4,
        splitLongWords=True,
        wordWrap="CJK",
    )
    meta_parts = [f"Mode: {mode}", f"First word: {'on' if show_first_word else 'off'}"]
    if any(unit.timestamp for unit in source_units):
        meta_parts.append(f"Timestamps: {'on' if show_timestamps else 'off'}")

    story = [
        Paragraph(escape("Dictation Worksheet"), title_style),
        Paragraph(escape(f"Source: {original_name}"), meta_style),
        Paragraph(escape(" | ".join(meta_parts)), meta_style),
        Spacer(1, 3),
    ]

    for unit in source_units:
        if mode == WORD_MODE:
            segments = build_word_mode_segments(
                unit,
                show_first_word=show_first_word,
                body_style=body_style,
                show_timestamps=show_timestamps,
            )
            story.append(
                WordMaskFlowable(
                    segments=segments,
                    body_style=body_style,
                )
            )
            continue

        segments = build_sentence_mode_segments(
            unit,
            show_first_word=show_first_word,
            body_style=body_style,
            show_timestamps=show_timestamps,
        )
        story.append(
            WordMaskFlowable(
                segments=segments,
                body_style=body_style,
            )
        )
        continue

    doc.build(story)
    return buffer.getvalue()


def build_answer_key_pdf(
    units: Iterable[TextUnit],
    original_name: str,
    show_timestamps: bool,
) -> bytes:
    register_pdf_font()
    source_units = [unit for unit in units if unit.text]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"{Path(original_name).stem} answer key",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "AnswerKeyTitle",
        parent=styles["Title"],
        fontName=PDF_FONT_NAME,
        fontSize=16,
        leading=20,
        spaceAfter=6,
    )
    meta_style = ParagraphStyle(
        "AnswerKeyMeta",
        parent=styles["Normal"],
        fontName=PDF_FONT_NAME,
        fontSize=9,
        leading=12,
        spaceAfter=2,
        textColor=colors.HexColor("#666666"),
    )
    body_style = ParagraphStyle(
        "AnswerKeyBody",
        parent=styles["Normal"],
        fontName=PDF_FONT_NAME,
        fontSize=13,
        leading=22,
        spaceAfter=4,
        splitLongWords=True,
        wordWrap="CJK",
    )

    meta_parts = ["Type: answer key"]
    if any(unit.timestamp for unit in source_units):
        meta_parts.append(f"Timestamps: {'on' if show_timestamps else 'off'}")

    story = [
        Paragraph(escape("Answer Key"), title_style),
        Paragraph(escape(f"Source: {original_name}"), meta_style),
        Paragraph(escape(" | ".join(meta_parts)), meta_style),
        Spacer(1, 3),
    ]

    for unit in source_units:
        story.append(build_answer_key_paragraph(unit, show_timestamps, body_style))

    doc.build(story)
    return buffer.getvalue()


def build_download_filename(original_name: str) -> str:
    stem = sanitize_filename_component(Path(original_name).stem)
    return f"{stem}_dictation.pdf"


def build_answer_key_filename(original_name: str) -> str:
    stem = sanitize_filename_component(Path(original_name).stem)
    return f"{stem}_answer_key.pdf"


def ensure_unique_filename(filename: str, used_names: set[str]) -> str:
    if filename not in used_names:
        used_names.add(filename)
        return filename

    path = Path(filename)
    index = 2
    while True:
        candidate = f"{path.stem}_{index}{path.suffix}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        index += 1


def build_zip_archive(files: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    used_names: set[str] = set()

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for filename, file_bytes in files:
            archive_name = ensure_unique_filename(filename, used_names)
            zip_file.writestr(archive_name, file_bytes)

    return buffer.getvalue()


def build_zip_download_filename(source_names: Iterable[str], kind: str) -> str:
    names = list(source_names)
    if len(names) == 1:
        stem = sanitize_filename_component(Path(names[0]).stem)
        if kind == "dictation":
            return f"{stem}_worksheets.zip"
        return f"{stem}_answer_keys.zip"

    if kind == "dictation":
        return "dictation_worksheets.zip"
    return "dictation_answer_keys.zip"


def main() -> None:
    st.set_page_config(page_title="영어 딕테이션 PDF 만들기", layout="centered")
    st.title("영어 딕테이션 PDF 만들기")
    st.write("자막이나 스크립트를 넣으면 문제지와 답지를 바로 만들 수 있어요.")

    st.subheader("입력")

    input_mode = st.radio(
        "입력 방식",
        [FILE_INPUT_MODE, DIRECT_INPUT_MODE],
        horizontal=True,
    )

    source_name = ""
    raw_text = ""
    source_format = PLAIN_TEXT_FORMAT
    text_pattern = TEXT_PATTERN_BASIC
    prepared_sources: list[PreparedSource] = []

    if input_mode == FILE_INPUT_MODE:
        uploaded_files = st.file_uploader(
            "자막 또는 스크립트 파일",
            type=["txt", "srt"],
            accept_multiple_files=True,
        )
        if not uploaded_files:
            st.info("`.txt` 또는 `.srt` 파일을 하나 이상 올려주세요.")
            return

        has_text_files = any(not is_srt_file(uploaded_file.name) for uploaded_file in uploaded_files)
        if has_text_files:
            text_pattern = st.radio(
                "TXT 파일 종류",
                [TEXT_PATTERN_BASIC, TEXT_PATTERN_PODSCRIPTS],
                horizontal=True,
            )
    else:
        source_format = st.radio(
            "붙여 넣는 내용",
            [PLAIN_TEXT_FORMAT, SRT_FORMAT],
            horizontal=True,
        )
        title = st.text_input("제목", value="영어 딕테이션")
        raw_text = st.text_area(
            "텍스트",
            height=220,
            placeholder="자막이나 스크립트를 여기에 붙여 넣으세요.",
        )

        if not raw_text.strip():
            st.info("자막이나 스크립트를 붙여 넣어주세요.")
            return

        source_name = build_direct_input_name(title, source_format)

    if source_format == PLAIN_TEXT_FORMAT:
        text_pattern = st.radio(
            "텍스트 종류",
            [TEXT_PATTERN_BASIC, TEXT_PATTERN_PODSCRIPTS],
            horizontal=True,
        )

    st.subheader("옵션")
    mode = st.radio("빈칸 방식", [WORD_MODE, SENTENCE_MODE], horizontal=True)
    show_first_word = st.checkbox("각 문장의 첫 단어 남기기", value=False)

    if input_mode == FILE_INPUT_MODE:
        skipped_files: list[str] = []
        for uploaded_file in uploaded_files:
            file_name = uploaded_file.name
            file_text = decode_text(uploaded_file.getvalue())
            units = extract_units(file_name, file_text, text_pattern=text_pattern)
            if not units:
                skipped_files.append(file_name)
                continue
            prepared_sources.append(PreparedSource(source_name=file_name, units=units))

        if not prepared_sources:
            st.error("읽을 수 있는 내용이 없어요. 파일 형식이나 텍스트를 다시 확인해주세요.")
            return

        if skipped_files:
            skipped_list = ", ".join(skipped_files)
            st.warning(f"일부 파일은 읽지 못해 제외했어요: {skipped_list}")
    else:
        units = extract_units(source_name, raw_text, text_pattern=text_pattern)
        if not units:
            st.error("읽을 수 있는 내용이 없어요. 파일 형식이나 텍스트를 다시 확인해주세요.")
            return
        prepared_sources.append(PreparedSource(source_name=source_name, units=units))

    has_timestamps = any(
        unit.timestamp
        for prepared_source in prepared_sources
        for unit in prepared_source.units
    )
    show_timestamps = st.checkbox("시간 표시하기", value=True) if has_timestamps else False

    preview_source = prepared_sources[0]
    if input_mode == FILE_INPUT_MODE and len(prepared_sources) > 1:
        preview_index = st.selectbox(
            "미리볼 파일",
            range(len(prepared_sources)),
            format_func=lambda index: prepared_sources[index].source_name,
        )
        preview_source = prepared_sources[preview_index]

    preview_units = [
        transform_unit(unit, mode=mode, show_first_word=show_first_word)
        for unit in preview_source.units
    ]
    preview_text = build_preview_text(preview_units, show_timestamps)

    st.subheader("미리보기")
    st.text_area("변환 결과", preview_text, height=320, label_visibility="collapsed")

    st.subheader("다운로드")
    download_col1, download_col2 = st.columns(2)
    if input_mode == FILE_INPUT_MODE:
        worksheet_files = [
            (
                build_download_filename(prepared_source.source_name),
                build_pdf(
                    units=prepared_source.units,
                    original_name=prepared_source.source_name,
                    mode=mode,
                    show_first_word=show_first_word,
                    show_timestamps=show_timestamps,
                ),
            )
            for prepared_source in prepared_sources
        ]
        answer_key_files = [
            (
                build_answer_key_filename(prepared_source.source_name),
                build_answer_key_pdf(
                    units=prepared_source.units,
                    original_name=prepared_source.source_name,
                    show_timestamps=show_timestamps,
                ),
            )
            for prepared_source in prepared_sources
        ]
        worksheet_zip = build_zip_archive(worksheet_files)
        answer_key_zip = build_zip_archive(answer_key_files)
        source_names = [prepared_source.source_name for prepared_source in prepared_sources]

        download_col1.download_button(
            label="문제지 ZIP 받기",
            data=worksheet_zip,
            file_name=build_zip_download_filename(source_names, kind="dictation"),
            mime="application/zip",
            use_container_width=True,
        )
        download_col2.download_button(
            label="답지 ZIP 받기",
            data=answer_key_zip,
            file_name=build_zip_download_filename(source_names, kind="answer_key"),
            mime="application/zip",
            use_container_width=True,
        )
    else:
        pdf_bytes = build_pdf(
            units=preview_source.units,
            original_name=preview_source.source_name,
            mode=mode,
            show_first_word=show_first_word,
            show_timestamps=show_timestamps,
        )
        answer_key_bytes = build_answer_key_pdf(
            units=preview_source.units,
            original_name=preview_source.source_name,
            show_timestamps=show_timestamps,
        )
        download_col1.download_button(
            label="문제지 PDF 받기",
            data=pdf_bytes,
            file_name=build_download_filename(preview_source.source_name),
            mime="application/pdf",
            use_container_width=True,
        )
        download_col2.download_button(
            label="답지 PDF 받기",
            data=answer_key_bytes,
            file_name=build_answer_key_filename(preview_source.source_name),
            mime="application/pdf",
            use_container_width=True,
        )


if __name__ == "__main__":
    main()
