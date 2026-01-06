"""Confluence-specific text preprocessing module.

Supports Confluence wiki markup syntax for rich formatting:

**Panel Macros** (info, note, warning, tip):
    {info}This is an info panel{info}
    {note}This is a note{note}
    {warning}This is a warning{warning}
    {tip}This is a tip{tip}

    With titles:
    {info:title=Important}Content here{info}

**Panel Macro** (custom colors):
    {panel}Default panel{panel}
    {panel:title=My Panel|bgColor=#f0f0f0}Custom panel{panel}

**Expand Macro** (collapsible sections):
    {expand}Hidden content{expand}
    {expand:Click to see more}Hidden content{expand}

**Status Lozenge**:
    {status:colour=Green|title=Done}
    {status:colour=Red|title=Blocked|subtle=true}
    Colors: Grey, Red, Yellow, Green, Blue

**Code Blocks**:
    {code:python}
    def hello():
        print("Hello")
    {code}

    Or standard markdown:
    ```python
    def hello():
        print("Hello")
    ```

**Horizontal Rule**:
    ---

**Tables**: Standard markdown tables are converted with proper header styling.
"""

import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .base import BasePreprocessor

logger = logging.getLogger("mcp-atlassian")


def markdown_to_confluence_storage(markdown_text: str) -> str:
    """
    Convert Markdown with Confluence wiki markup to Confluence storage format (XHTML).

    This is a single-pass converter that handles both Confluence wiki macros
    and standard Markdown, outputting valid Confluence storage format XHTML.

    Args:
        markdown_text: Text with Markdown and optional Confluence wiki markup

    Returns:
        Confluence storage format (XHTML) string
    """
    if not markdown_text:
        return ""

    lines = markdown_text.split("\n")
    result: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Check for macro starts: {info}, {note}, {warning}, {tip}, {panel}, {expand}, {code}
        macro_match = re.match(
            r"^\{(info|note|warning|tip|panel|expand|code)(?::([^}]*))?\}\s*$",
            line.strip(),
            re.IGNORECASE,
        )
        if macro_match:
            macro_name = macro_match.group(1).lower()
            macro_params = macro_match.group(2) or ""
            i, macro_xml = _parse_block_macro(lines, i, macro_name, macro_params)
            result.append(macro_xml)
            continue

        # Check for inline status macro: {status:...}
        if "{status:" in line:
            line = _process_inline_status(line)

        # Check for horizontal rule
        if re.match(r"^-{3,}\s*$", line.strip()):
            result.append("<hr />")
            i += 1
            continue

        # Check for markdown code block: ```
        if line.strip().startswith("```"):
            i, code_xml = _parse_markdown_code_block(lines, i)
            result.append(code_xml)
            continue

        # Check for headers
        header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if header_match:
            level = len(header_match.group(1))
            text = _process_inline(header_match.group(2))
            result.append(f"<h{level}>{text}</h{level}>")
            i += 1
            continue

        # Check for bullet list
        if re.match(r"^[-*+]\s+", line):
            i, list_html = _parse_list(lines, i, "ul")
            result.append(list_html)
            continue

        # Check for numbered list
        if re.match(r"^\d+\.\s+", line):
            i, list_html = _parse_list(lines, i, "ol")
            result.append(list_html)
            continue

        # Check for table
        if "|" in line and i + 1 < len(lines) and re.match(r"^\|[-:\s|]+\|$", lines[i + 1].strip()):
            i, table_html = _parse_table(lines, i)
            result.append(table_html)
            continue

        # Skip empty lines
        if not line.strip():
            i += 1
            continue

        # Regular paragraph - collect consecutive non-empty lines
        para_lines = []
        while i < len(lines) and lines[i].strip() and not _is_block_start(lines[i]):
            para_lines.append(lines[i].strip())
            i += 1
        if para_lines:
            para_text = " ".join(para_lines)
            result.append(f"<p>{_process_inline(para_text)}</p>")

    return "\n".join(result)


def _is_block_start(line: str) -> bool:
    """Check if a line starts a new block element."""
    stripped = line.strip()
    if not stripped:
        return False
    # Macro start
    if re.match(r"^\{(info|note|warning|tip|panel|expand|code)(?::|}).*", stripped, re.IGNORECASE):
        return True
    # Header
    if re.match(r"^#{1,6}\s+", stripped):
        return True
    # List
    if re.match(r"^[-*+]\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
        return True
    # Horizontal rule
    if re.match(r"^-{3,}$", stripped):
        return True
    # Code block
    if stripped.startswith("```"):
        return True
    # Table (check for pipe at start)
    if stripped.startswith("|"):
        return True
    return False


def _parse_block_macro(
    lines: list[str], start_idx: int, macro_name: str, params_str: str
) -> tuple[int, str]:
    """
    Parse a block macro from {macro} to {macro}.

    Returns (new_index, storage_xml).
    """
    i = start_idx + 1
    body_lines: list[str] = []

    # Find the closing tag
    close_pattern = rf"^\{{{macro_name}\}}\s*$"
    while i < len(lines):
        if re.match(close_pattern, lines[i].strip(), re.IGNORECASE):
            i += 1
            break
        body_lines.append(lines[i])
        i += 1

    body_content = "\n".join(body_lines).strip()

    # Handle code macro specially - it uses plain-text-body
    if macro_name == "code":
        return i, _build_code_macro(params_str, body_content)

    # For other macros, recursively process the body
    body_html = markdown_to_confluence_storage(body_content) if body_content else "<p></p>"

    # Build the macro XML
    if macro_name in ("info", "note", "warning", "tip"):
        return i, _build_panel_type_macro(macro_name, params_str, body_html)
    elif macro_name == "panel":
        return i, _build_panel_macro(params_str, body_html)
    elif macro_name == "expand":
        return i, _build_expand_macro(params_str, body_html)

    return i, body_html


def _build_panel_type_macro(macro_name: str, params_str: str, body_html: str) -> str:
    """Build info/note/warning/tip macro."""
    params = _parse_macro_params(params_str)
    params_xml = ""
    if params.get("title"):
        params_xml = f'<ac:parameter ac:name="title">{_escape_xml(params["title"])}</ac:parameter>'

    return (
        f'<ac:structured-macro ac:name="{macro_name}">'
        f"{params_xml}"
        f"<ac:rich-text-body>{body_html}</ac:rich-text-body>"
        f"</ac:structured-macro>"
    )


def _build_panel_macro(params_str: str, body_html: str) -> str:
    """Build generic panel macro."""
    params = _parse_macro_params(params_str)
    params_xml = ""

    if params.get("title"):
        params_xml += f'<ac:parameter ac:name="title">{_escape_xml(params["title"])}</ac:parameter>'
    if params.get("bgColor") or params.get("bgcolor"):
        bg = params.get("bgColor") or params.get("bgcolor")
        params_xml += f'<ac:parameter ac:name="bgColor">{_escape_xml(bg)}</ac:parameter>'
    if params.get("borderStyle") or params.get("borderstyle"):
        bs = params.get("borderStyle") or params.get("borderstyle")
        params_xml += f'<ac:parameter ac:name="borderStyle">{_escape_xml(bs)}</ac:parameter>'
    if params.get("borderColor") or params.get("bordercolor"):
        bc = params.get("borderColor") or params.get("bordercolor")
        params_xml += f'<ac:parameter ac:name="borderColor">{_escape_xml(bc)}</ac:parameter>'

    return (
        f'<ac:structured-macro ac:name="panel">'
        f"{params_xml}"
        f"<ac:rich-text-body>{body_html}</ac:rich-text-body>"
        f"</ac:structured-macro>"
    )


def _build_expand_macro(params_str: str, body_html: str) -> str:
    """Build expand (collapsible) macro."""
    title = params_str.strip() if params_str else "Click here to expand..."

    return (
        f'<ac:structured-macro ac:name="expand">'
        f'<ac:parameter ac:name="title">{_escape_xml(title)}</ac:parameter>'
        f"<ac:rich-text-body>{body_html}</ac:rich-text-body>"
        f"</ac:structured-macro>"
    )


def _build_code_macro(params_str: str, code_content: str) -> str:
    """Build code block macro."""
    params: dict[str, str] = {}
    if params_str:
        if "=" in params_str:
            params = _parse_macro_params(params_str)
        else:
            # Just a language name
            params["language"] = params_str

    params_xml = ""
    if params.get("language"):
        params_xml += f'<ac:parameter ac:name="language">{_escape_xml(params["language"])}</ac:parameter>'
    if params.get("title"):
        params_xml += f'<ac:parameter ac:name="title">{_escape_xml(params["title"])}</ac:parameter>'
    if params.get("linenumbers"):
        params_xml += f'<ac:parameter ac:name="linenumbers">{params["linenumbers"]}</ac:parameter>'
    if params.get("collapse"):
        params_xml += f'<ac:parameter ac:name="collapse">{params["collapse"]}</ac:parameter>'

    return (
        f'<ac:structured-macro ac:name="code">'
        f"{params_xml}"
        f"<ac:plain-text-body><![CDATA[{code_content}]]></ac:plain-text-body>"
        f"</ac:structured-macro>"
    )


def _parse_markdown_code_block(lines: list[str], start_idx: int) -> tuple[int, str]:
    """Parse a markdown-style code block (triple backticks)."""
    first_line = lines[start_idx].strip()
    language = first_line[3:].strip()  # Everything after ```

    i = start_idx + 1
    code_lines: list[str] = []

    while i < len(lines):
        if lines[i].strip() == "```":
            i += 1
            break
        code_lines.append(lines[i])
        i += 1

    code_content = "\n".join(code_lines)
    params_str = language if language else ""

    return i, _build_code_macro(params_str, code_content)


def _process_inline_status(line: str) -> str:
    """Process inline status macros in a line."""
    pattern = r"\{status:([^}]+)\}"

    def replace_status(match: re.Match) -> str:
        params = _parse_macro_params(match.group(1))

        colour = params.get("colour") or params.get("color") or "Grey"
        title = params.get("title") or colour
        subtle = params.get("subtle")

        params_xml = f'<ac:parameter ac:name="colour">{_escape_xml(colour)}</ac:parameter>'
        params_xml += f'<ac:parameter ac:name="title">{_escape_xml(title)}</ac:parameter>'
        if subtle:
            params_xml += f'<ac:parameter ac:name="subtle">{subtle}</ac:parameter>'

        return f'<ac:structured-macro ac:name="status">{params_xml}</ac:structured-macro>'

    return re.sub(pattern, replace_status, line, flags=re.IGNORECASE)


def _parse_list(lines: list[str], start_idx: int, list_type: str) -> tuple[int, str]:
    """Parse a bullet or numbered list."""
    items: list[str] = []
    i = start_idx
    pattern = r"^[-*+]\s+" if list_type == "ul" else r"^\d+\.\s+"

    while i < len(lines):
        line = lines[i]
        if re.match(pattern, line):
            item_text = re.sub(pattern, "", line).strip()
            items.append(f"<li><p>{_process_inline(item_text)}</p></li>")
            i += 1
        else:
            break

    return i, f"<{list_type}>{''.join(items)}</{list_type}>"


def _parse_table(lines: list[str], start_idx: int) -> tuple[int, str]:
    """Parse a markdown table."""
    i = start_idx
    rows: list[str] = []

    # Header row
    header_line = lines[i].strip()
    header_cells = [c.strip() for c in header_line.split("|")[1:-1]]  # Remove empty first/last from split
    header_html = "".join(
        f'<th data-highlight-colour="#f4f5f7"><p>{_process_inline(c)}</p></th>'
        for c in header_cells
    )
    rows.append(f"<tr>{header_html}</tr>")
    i += 1

    # Skip separator row (|---|---|)
    if i < len(lines) and re.match(r"^\|[-:\s|]+\|$", lines[i].strip()):
        i += 1

    # Data rows
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.split("|")[1:-1]]
        cells_html = "".join(f"<td><p>{_process_inline(c)}</p></td>" for c in cells)
        rows.append(f"<tr>{cells_html}</tr>")
        i += 1

    return i, f"<table><tbody>{''.join(rows)}</tbody></table>"


def _process_inline(text: str) -> str:
    """Process inline markdown formatting."""
    if not text:
        return ""

    # Process inline status macros first
    text = _process_inline_status(text)

    # Bold: **text** or __text__
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)

    # Italic: *text* or _text_
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"<em>\1</em>", text)

    # Inline code: `code`
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    # Links: [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    return text


def _parse_macro_params(params_str: str) -> dict[str, str]:
    """Parse macro parameters from 'key=value|key2=value2' format."""
    params: dict[str, str] = {}
    if not params_str:
        return params

    # Handle both 'title=X' format and 'title=X|bgColor=Y' format
    for part in params_str.split("|"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            params[key.strip()] = value.strip()

    return params


def _escape_xml(text: str) -> str:
    """Escape special XML characters."""
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


class ConfluencePreprocessor(BasePreprocessor):
    """Handles text preprocessing for Confluence content."""

    def __init__(self, base_url: str) -> None:
        """
        Initialize the Confluence text preprocessor.

        Args:
            base_url: Base URL for Confluence API
        """
        super().__init__(base_url=base_url)

    def markdown_to_confluence_storage(
        self, markdown_content: str, *, enable_heading_anchors: bool = False
    ) -> str:
        """
        Convert Markdown content to Confluence storage format (XHTML).

        This method supports both standard Markdown and Confluence wiki markup syntax
        for rich formatting. See module docstring for supported syntax.

        Args:
            markdown_content: Markdown text with optional Confluence wiki markup
            enable_heading_anchors: Whether to enable automatic heading anchor
                generation (default: False). Note: Currently not implemented
                in the custom converter.

        Returns:
            Confluence storage format (XHTML) string
        """
        # Use the new comprehensive converter
        return markdown_to_confluence_storage(markdown_content)

    # Confluence-specific methods can be added here
