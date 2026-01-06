"""Jira-specific text preprocessing module."""

import logging
import re
from typing import Any

from .base import BasePreprocessor

logger = logging.getLogger("mcp-atlassian")


def markdown_to_adf(
    markdown_text: str,
    attachments: dict[str, str] | None = None
) -> dict[str, Any]:
    """
    Convert Markdown text to Atlassian Document Format (ADF).

    Supports:
    - Headings (# ## ###)
    - Bold (**text**) and Italic (*text*)
    - Links [text](url) - Atlassian URLs become inlineCard smart links
    - Bullet lists (- item)
    - Numbered lists (1. item)
    - Tables (| col | col | with |---| separator)
    - Code blocks (```lang ... ```)
    - Inline code (`code`)
    - Horizontal rules (---)
    - Panels via special syntax: {note}content{note} or {panel:note}content{panel}
    - Expand/collapse sections: {expand:title}content{expand}
    - Images (!filename! or ![alt](url)) - converted to ADF media nodes if attachments provided

    Args:
        markdown_text: Text in Markdown format
        attachments: Optional dict mapping filenames to attachment IDs for image resolution

    Returns:
        ADF document as a dictionary
    """
    if not markdown_text:
        return {"version": 1, "type": "doc", "content": []}

    content = []
    lines = markdown_text.split('\n')
    i = 0

    while i < len(lines):
        line = lines[i]

        # Skip empty lines
        if not line.strip():
            i += 1
            continue

        # Check for expand start: {expand:title} or {expand}
        expand_match = re.match(r'^\{expand(?::([^}]*))?\}$', line.strip())
        if expand_match:
            expand_title = expand_match.group(1) or ""
            expand_content = []
            i += 1
            # Collect content until expand end
            while i < len(lines):
                if re.match(r'^\{expand\}$', lines[i].strip()):
                    i += 1
                    break
                expand_content.append(lines[i])
                i += 1
            # Parse expand content recursively
            expand_adf = markdown_to_adf('\n'.join(expand_content), attachments)
            content.append({
                "type": "expand",
                "attrs": {"title": expand_title},
                "content": expand_adf.get("content", [])
            })
            continue

        # Check for panel start: {panel:type} or {info}, {note}, {warning}, etc.
        panel_match = re.match(r'^\{(panel:)?(info|note|warning|success|error)\}$', line.strip())
        if panel_match:
            panel_type = panel_match.group(2)
            panel_content = []
            i += 1
            # Collect content until panel end
            while i < len(lines):
                if re.match(r'^\{(panel|info|note|warning|success|error)\}$', lines[i].strip()):
                    i += 1
                    break
                panel_content.append(lines[i])
                i += 1
            # Parse panel content recursively
            panel_adf = markdown_to_adf('\n'.join(panel_content), attachments)
            content.append({
                "type": "panel",
                "attrs": {"panelType": panel_type},
                "content": panel_adf.get("content", [])
            })
            continue

        # Check for code block start
        code_match = re.match(r'^```(\w*)$', line)
        if code_match:
            language = code_match.group(1) or None
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code_lines.append(lines[i])
                i += 1
            i += 1  # Skip closing ```

            code_block = {"type": "codeBlock", "content": [{"type": "text", "text": '\n'.join(code_lines)}]}
            if language:
                code_block["attrs"] = {"language": language}
            content.append(code_block)
            continue

        # Horizontal rule
        if re.match(r'^-{3,}$', line.strip()) or re.match(r'^\*{3,}$', line.strip()) or re.match(r'^_{3,}$', line.strip()):
            content.append({"type": "rule"})
            i += 1
            continue

        # Headings
        heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading_match:
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2)
            content.append({
                "type": "heading",
                "attrs": {"level": level},
                "content": _parse_inline_content(heading_text, attachments)
            })
            i += 1
            continue

        # Bullet list
        if re.match(r'^[-*+]\s+', line):
            list_items = []
            while i < len(lines) and re.match(r'^[-*+]\s+', lines[i]):
                item_text = re.sub(r'^[-*+]\s+', '', lines[i])
                # Handle checkbox syntax
                checkbox_match = re.match(r'^\[([xX ])\]\s*(.*)$', item_text)
                if checkbox_match:
                    item_text = checkbox_match.group(2)
                list_items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _parse_inline_content(item_text, attachments)}]
                })
                i += 1
            content.append({"type": "bulletList", "content": list_items})
            continue

        # Numbered list
        if re.match(r'^\d+\.\s+', line):
            list_items = []
            while i < len(lines) and re.match(r'^\d+\.\s+', lines[i]):
                item_text = re.sub(r'^\d+\.\s+', '', lines[i])
                list_items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _parse_inline_content(item_text, attachments)}]
                })
                i += 1
            content.append({"type": "orderedList", "content": list_items})
            continue

        # Markdown table: detect header row followed by separator row
        if re.match(r'^\|.+\|$', line.strip()):
            # Check if next line is a separator (|---|---|)
            if i + 1 < len(lines) and re.match(r'^\|[-:\s|]+\|$', lines[i + 1].strip()):
                table_rows = []

                # Parse header row
                header_line = line.strip()
                header_cells = [cell.strip() for cell in header_line.split('|')[1:-1]]
                header_row = {
                    "type": "tableRow",
                    "content": [
                        {
                            "type": "tableHeader",
                            "attrs": {},
                            "content": [{"type": "paragraph", "content": _parse_inline_content(cell, attachments)}]
                        }
                        for cell in header_cells
                    ]
                }
                table_rows.append(header_row)
                i += 2  # Skip header and separator rows

                # Parse data rows
                while i < len(lines) and re.match(r'^\|.+\|$', lines[i].strip()):
                    row_line = lines[i].strip()
                    row_cells = [cell.strip() for cell in row_line.split('|')[1:-1]]
                    data_row = {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableCell",
                                "attrs": {},
                                "content": [{"type": "paragraph", "content": _parse_inline_content(cell, attachments)}]
                            }
                            for cell in row_cells
                        ]
                    }
                    table_rows.append(data_row)
                    i += 1

                content.append({
                    "type": "table",
                    "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
                    "content": table_rows
                })
                continue

        # Regular paragraph
        para_lines = [line]
        i += 1
        # Collect continuation lines (non-empty, non-special)
        while i < len(lines):
            next_line = lines[i]
            if not next_line.strip():
                break
            if re.match(r'^(#{1,6}|[-*+]|\d+\.)\s+', next_line):
                break
            if re.match(r'^-{3,}$|^\*{3,}$|^_{3,}$', next_line.strip()):
                break
            if re.match(r'^```', next_line):
                break
            if re.match(r'^\{(panel:)?(info|note|warning|success|error)\}$', next_line.strip()):
                break
            # Don't merge table rows into paragraphs
            if re.match(r'^\|.+\|$', next_line.strip()):
                break
            para_lines.append(next_line)
            i += 1

        para_text = ' '.join(para_lines)
        if para_text.strip():
            content.append({
                "type": "paragraph",
                "content": _parse_inline_content(para_text, attachments)
            })

    return {"version": 1, "type": "doc", "content": content}


def _parse_inline_content(
    text: str,
    attachments: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """
    Parse inline markdown elements (bold, italic, links, code, images).

    Args:
        text: Text with inline markdown formatting
        attachments: Optional dict mapping filenames to attachment IDs for image resolution

    Returns:
        List of ADF inline nodes
    """
    if not text:
        return []

    result = []

    # Pattern for inline elements: bold, italic, links, code, images
    # Order matters - more specific patterns first
    patterns = [
        # Images: ![alt](url) or !filename!
        (r'!\[([^\]]*)\]\(([^)]+)\)', 'image_md'),
        (r'!([^!\s|]+)(?:\|[^!]*)?\!', 'image_jira'),
        # Links: [text](url)
        (r'\[([^\]]+)\]\(([^)]+)\)', 'link'),
        # Bare Atlassian URLs (Jira issues, Confluence pages) - convert to inlineCard
        (r'(https://[^\s]*\.atlassian\.net/(?:browse/[A-Z]+-\d+|wiki/[^\s]*))', 'bare_atlassian_url'),
        # Bold + Italic: ***text*** or ___text___
        (r'\*\*\*([^*]+)\*\*\*', 'bold_italic'),
        (r'___([^_]+)___', 'bold_italic'),
        # Bold: **text** or __text__
        (r'\*\*([^*]+)\*\*', 'bold'),
        (r'__([^_]+)__', 'bold'),
        # Italic: *text* or _text_
        (r'(?<!\*)\*([^*]+)\*(?!\*)', 'italic'),
        (r'(?<!_)_([^_]+)_(?!_)', 'italic'),
        # Inline code: `code`
        (r'`([^`]+)`', 'code'),
    ]

    # Find all matches with their positions
    matches = []
    for pattern, mark_type in patterns:
        for match in re.finditer(pattern, text):
            matches.append((match.start(), match.end(), match, mark_type))

    # Sort by position
    matches.sort(key=lambda x: x[0])

    # Remove overlapping matches (keep first)
    filtered_matches = []
    last_end = 0
    for start, end, match, mark_type in matches:
        if start >= last_end:
            filtered_matches.append((start, end, match, mark_type))
            last_end = end

    # Build result
    pos = 0
    for start, end, match, mark_type in filtered_matches:
        # Add plain text before this match
        if start > pos:
            plain_text = text[pos:start]
            if plain_text:
                result.append({"type": "text", "text": plain_text})

        if mark_type == 'link':
            link_text = match.group(1)
            link_url = match.group(2)
            # Use inlineCard for Atlassian URLs (Jira issues, Confluence pages)
            if re.search(r'atlassian\.net/(?:browse/|wiki/|jira/)', link_url):
                result.append({
                    "type": "inlineCard",
                    "attrs": {"url": link_url}
                })
            else:
                result.append({
                    "type": "text",
                    "text": link_text,
                    "marks": [{"type": "link", "attrs": {"href": link_url}}]
                })
        elif mark_type == 'bold':
            result.append({
                "type": "text",
                "text": match.group(1),
                "marks": [{"type": "strong"}]
            })
        elif mark_type == 'italic':
            result.append({
                "type": "text",
                "text": match.group(1),
                "marks": [{"type": "em"}]
            })
        elif mark_type == 'bold_italic':
            result.append({
                "type": "text",
                "text": match.group(1),
                "marks": [{"type": "strong"}, {"type": "em"}]
            })
        elif mark_type == 'code':
            result.append({
                "type": "text",
                "text": match.group(1),
                "marks": [{"type": "code"}]
            })
        elif mark_type == 'bare_atlassian_url':
            result.append({
                "type": "inlineCard",
                "attrs": {"url": match.group(1)}
            })
        elif mark_type in ('image_md', 'image_jira'):
            # Handle images - use proper ADF media nodes if we have attachment info
            filename = None
            if mark_type == 'image_jira':
                # Jira syntax: !filename! or !filename|width=X!
                filename = match.group(1)
            else:
                # Markdown syntax: ![alt](url)
                url = match.group(2)
                if not url.startswith('http'):
                    filename = url

            # Try to resolve to proper ADF media node if we have attachment mapping
            if filename and attachments and filename in attachments:
                attachment_id = attachments[filename]
                # Use mediaInline for inline images in ADF
                result.append({
                    "type": "mediaInline",
                    "attrs": {
                        "id": attachment_id,
                        "type": "file",
                        "collection": ""
                    }
                })
            elif mark_type == 'image_md' and match.group(2).startswith('http'):
                # External URL image - convert to link
                alt_text = match.group(1)
                url = match.group(2)
                result.append({
                    "type": "text",
                    "text": alt_text or url,
                    "marks": [{"type": "link", "attrs": {"href": url}}]
                })
            else:
                # No attachment mapping available - preserve as placeholder text
                # This will show as text but won't break the update
                if filename:
                    result.append({
                        "type": "text",
                        "text": f"[Image: {filename}]"
                    })
                else:
                    result.append({
                        "type": "text",
                        "text": match.group(0)
                    })

        pos = end

    # Add remaining text
    if pos < len(text):
        remaining = text[pos:]
        if remaining:
            result.append({"type": "text", "text": remaining})

    # If no matches, return plain text
    if not result and text:
        result.append({"type": "text", "text": text})

    return result


class JiraPreprocessor(BasePreprocessor):
    """Handles text preprocessing for Jira content."""

    def __init__(
        self, base_url: str = "", disable_translation: bool = False, **kwargs: Any
    ) -> None:
        """
        Initialize the Jira text preprocessor.

        Args:
            base_url: Base URL for Jira API
            disable_translation: If True, disable markup translation between formats
            **kwargs: Additional arguments for the base class
        """
        super().__init__(base_url=base_url, **kwargs)
        self.disable_translation = disable_translation

    def clean_jira_text(self, text: str) -> str:
        """
        Clean Jira text content by:
        1. Processing user mentions and links
        2. Converting Jira markup to markdown (if translation enabled)
        3. Converting HTML/wiki markup to markdown (if translation enabled)
        """
        if not text:
            return ""

        # Process user mentions
        mention_pattern = r"\[~accountid:(.*?)\]"
        text = self._process_mentions(text, mention_pattern)

        # Process Jira smart links
        text = self._process_smart_links(text)

        # Convert markup only if translation is enabled
        if not self.disable_translation:
            # First convert any Jira markup to Markdown
            text = self.jira_to_markdown(text)

            # Then convert any remaining HTML to markdown
            text = self._convert_html_to_markdown(text)

        return text.strip()

    def _process_mentions(self, text: str, pattern: str) -> str:
        """
        Process user mentions in text.

        Args:
            text: The text containing mentions
            pattern: Regular expression pattern to match mentions

        Returns:
            Text with mentions replaced with display names
        """
        mentions = re.findall(pattern, text)
        for account_id in mentions:
            try:
                # Note: This is a placeholder - actual user fetching should be injected
                display_name = f"User:{account_id}"
                text = text.replace(f"[~accountid:{account_id}]", display_name)
            except Exception as e:
                logger.error(f"Error processing mention for {account_id}: {str(e)}")
        return text

    def _process_smart_links(self, text: str) -> str:
        """Process Jira/Confluence smart links."""
        # Pattern matches: [text|url|smart-link]
        link_pattern = r"\[(.*?)\|(.*?)\|smart-link\]"
        matches = re.finditer(link_pattern, text)

        for match in matches:
            full_match = match.group(0)
            link_text = match.group(1)
            link_url = match.group(2)

            # Extract issue key if it's a Jira issue link
            issue_key_match = re.search(r"browse/([A-Z]+-\d+)", link_url)
            # Check if it's a Confluence wiki link
            confluence_match = re.search(
                r"wiki/spaces/.+?/pages/\d+/(.+?)(?:\?|$)", link_url
            )

            if issue_key_match:
                issue_key = issue_key_match.group(1)
                clean_url = f"{self.base_url}/browse/{issue_key}"
                text = text.replace(full_match, f"[{issue_key}]({clean_url})")
            elif confluence_match:
                url_title = confluence_match.group(1)
                readable_title = url_title.replace("+", " ")
                readable_title = re.sub(r"^[A-Z]+-\d+\s+", "", readable_title)
                text = text.replace(full_match, f"[{readable_title}]({link_url})")
            else:
                clean_url = link_url.split("?")[0]
                text = text.replace(full_match, f"[{link_text}]({clean_url})")

        return text

    def jira_to_markdown(self, input_text: str) -> str:
        """
        Convert Jira markup to Markdown format.

        Args:
            input_text: Text in Jira markup format

        Returns:
            Text in Markdown format (or original text if translation disabled)
        """
        if not input_text:
            return ""

        if self.disable_translation:
            return input_text

        # Block quotes
        output = re.sub(r"^bq\.(.*?)$", r"> \1\n", input_text, flags=re.MULTILINE)

        # Text formatting (bold, italic)
        output = re.sub(
            r"([*_])(.*?)\1",
            lambda match: ("**" if match.group(1) == "*" else "*")
            + match.group(2)
            + ("**" if match.group(1) == "*" else "*"),
            output,
        )

        # Multi-level numbered list
        output = re.sub(
            r"^((?:#|-|\+|\*)+) (.*)$",
            lambda match: self._convert_jira_list_to_markdown(match),
            output,
            flags=re.MULTILINE,
        )

        # Headers
        output = re.sub(
            r"^h([0-6])\.(.*)$",
            lambda match: "#" * int(match.group(1)) + match.group(2),
            output,
            flags=re.MULTILINE,
        )

        # Inline code
        output = re.sub(r"\{\{([^}]+)\}\}", r"`\1`", output)

        # Citation
        output = re.sub(r"\?\?((?:.[^?]|[^?].)+)\?\?", r"<cite>\1</cite>", output)

        # Inserted text
        output = re.sub(r"\+([^+]*)\+", r"<ins>\1</ins>", output)

        # Superscript
        output = re.sub(r"\^([^^]*)\^", r"<sup>\1</sup>", output)

        # Subscript
        output = re.sub(r"~([^~]*)~", r"<sub>\1</sub>", output)

        # Strikethrough
        output = re.sub(r"-([^-]*)-", r"-\1-", output)

        # Code blocks with optional language specification
        output = re.sub(
            r"\{code(?::([a-z]+))?\}([\s\S]*?)\{code\}",
            r"```\1\n\2\n```",
            output,
            flags=re.MULTILINE,
        )

        # No format
        output = re.sub(r"\{noformat\}([\s\S]*?)\{noformat\}", r"```\n\1\n```", output)

        # Quote blocks
        output = re.sub(
            r"\{quote\}([\s\S]*)\{quote\}",
            lambda match: "\n".join(
                [f"> {line}" for line in match.group(1).split("\n")]
            ),
            output,
            flags=re.MULTILINE,
        )

        # Images with alt text
        output = re.sub(
            r"!([^|\n\s]+)\|([^\n!]*)alt=([^\n!\,]+?)(,([^\n!]*))?!",
            r"![\3](\1)",
            output,
        )

        # Images with other parameters (ignore them)
        output = re.sub(r"!([^|\n\s]+)\|([^\n!]*)!", r"![](\1)", output)

        # Images without parameters
        output = re.sub(r"!([^\n\s!]+)!", r"![](\1)", output)

        # Links
        output = re.sub(r"\[([^|]+)\|(.+?)\]", r"[\1](\2)", output)
        output = re.sub(r"\[(.+?)\]([^\(]+)", r"<\1>\2", output)

        # Colored text
        output = re.sub(
            r"\{color:([^}]+)\}([\s\S]*?)\{color\}",
            r"<span style=\"color:\1\">\2</span>",
            output,
            flags=re.MULTILINE,
        )

        # Convert Jira table headers (||) to markdown table format
        lines = output.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]

            if "||" in line:
                # Replace Jira table headers
                lines[i] = lines[i].replace("||", "|")

                # Add a separator line for markdown tables
                header_cells = lines[i].count("|") - 1
                if header_cells > 0:
                    separator_line = "|" + "---|" * header_cells
                    lines.insert(i + 1, separator_line)
                    i += 1  # Skip the newly inserted line in next iteration

            i += 1

        # Rejoin the lines
        output = "\n".join(lines)

        return output

    def markdown_to_jira(self, input_text: str) -> str:
        """
        Convert Markdown syntax to Jira markup syntax.

        Args:
            input_text: Text in Markdown format

        Returns:
            Text in Jira markup format (or original text if translation disabled)
        """
        if not input_text:
            return ""

        if self.disable_translation:
            return input_text

        # Save code blocks to prevent recursive processing
        code_blocks = []
        inline_codes = []

        # Extract code blocks
        def save_code_block(match: re.Match) -> str:
            """
            Process and save a code block.

            Args:
                match: Regex match object containing the code block

            Returns:
                Jira-formatted code block
            """
            syntax = match.group(1) or ""
            content = match.group(2)
            code = "{code"
            if syntax:
                code += ":" + syntax
            code += "}" + content + "{code}"
            code_blocks.append(code)
            return str(code)  # Ensure we return a string

        # Extract inline code
        def save_inline_code(match: re.Match) -> str:
            """
            Process and save inline code.

            Args:
                match: Regex match object containing the inline code

            Returns:
                Jira-formatted inline code
            """
            content = match.group(1)
            code = "{{" + content + "}}"
            inline_codes.append(code)
            return str(code)  # Ensure we return a string

        # Save code sections temporarily
        output = re.sub(r"```(\w*)\n([\s\S]+?)```", save_code_block, input_text)
        output = re.sub(r"`([^`]+)`", save_inline_code, output)

        # Headers with = or - underlines
        output = re.sub(
            r"^(.*?)\n([=-])+$",
            lambda match: f"h{1 if match.group(2)[0] == '=' else 2}. {match.group(1)}",
            output,
            flags=re.MULTILINE,
        )

        # Headers with # prefix - require space after # to distinguish from Jira lists
        # Fixes issue #786: #item should not become h1.item (it's a Jira numbered list)
        output = re.sub(
            r"^([#]+) (.*)$",
            lambda match: f"h{len(match.group(1))}. " + match.group(2),
            output,
            flags=re.MULTILINE,
        )

        # Bold and italic - skip lines starting with asterisks+space (Jira list syntax)
        # Fixes issue #786: ** item should not be converted (it's a Jira nested list)
        def convert_bold_italic_line(line: str) -> str:
            # Skip if line starts with asterisks/underscores followed by space (list syntax)
            if re.match(r"^[*_]+\s", line):
                return line
            # Apply bold/italic conversion
            return re.sub(
                r"([*_]+)(.*?)\1",
                lambda m: ("_" if len(m.group(1)) == 1 else "*")
                + m.group(2)
                + ("_" if len(m.group(1)) == 1 else "*"),
                line,
            )

        lines = output.split("\n")
        output = "\n".join(convert_bold_italic_line(line) for line in lines)

        # Multi-level bulleted list
        def bulleted_list_fn(match: re.Match) -> str:
            ident = len(match.group(1)) if match.group(1) else 0
            level = ident // 2 + 1
            return str("*" * level + " " + match.group(2))

        output = re.sub(
            r"^(\s+)?[-+*] (.*)$",
            bulleted_list_fn,
            output,
            flags=re.MULTILINE,
        )

        # Multi-level numbered list
        def numbered_list_fn(match: re.Match) -> str:
            ident = len(match.group(1)) if match.group(1) else 0
            level = ident // 2 + 1
            return str("#" * level + " " + match.group(2))

        output = re.sub(
            r"^(\s+)?\d+\. (.*)$",
            numbered_list_fn,
            output,
            flags=re.MULTILINE,
        )

        # HTML formatting tags to Jira markup
        tag_map = {"cite": "??", "del": "-", "ins": "+", "sup": "^", "sub": "~"}

        for tag, replacement in tag_map.items():
            output = re.sub(
                rf"<{tag}>(.*?)<\/{tag}>", rf"{replacement}\1{replacement}", output
            )

        # Colored text
        output = re.sub(
            r"<span style=\"color:(#[^\"]+)\">([\s\S]*?)</span>",
            r"{color:\1}\2{color}",
            output,
            flags=re.MULTILINE,
        )

        # Strikethrough
        output = re.sub(r"~~(.*?)~~", r"-\1-", output)

        # Images without alt text
        output = re.sub(r"!\[\]\(([^)\n\s]+)\)", r"!\1!", output)

        # Images with alt text
        output = re.sub(r"!\[([^\]\n]+)\]\(([^)\n\s]+)\)", r"!\2|alt=\1!", output)

        # Links
        output = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"[\1|\2]", output)
        output = re.sub(r"<([^>]+)>", r"[\1]", output)

        # Convert markdown tables to Jira table format
        lines = output.split("\n")
        i = 0
        while i < len(lines):
            if i < len(lines) - 1 and re.match(r"\|[-\s|]+\|", lines[i + 1]):
                # Convert header row to Jira format
                lines[i] = lines[i].replace("|", "||")
                # Remove the separator line
                lines.pop(i + 1)
            i += 1

        # Rejoin the lines
        output = "\n".join(lines)

        return output

    def _convert_jira_list_to_markdown(self, match: re.Match) -> str:
        """
        Helper method to convert Jira lists to Markdown format.

        Args:
            match: Regex match object containing the Jira list markup

        Returns:
            Markdown-formatted list item
        """
        jira_bullets = match.group(1)
        content = match.group(2)

        # Calculate indentation level based on number of symbols
        indent_level = len(jira_bullets) - 1
        indent = " " * (indent_level * 2)

        # Determine the marker based on the last character
        last_char = jira_bullets[-1]
        prefix = "1." if last_char == "#" else "-"

        return f"{indent}{prefix} {content}"
