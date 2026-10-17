from __future__ import annotations

from email.message import EmailMessage
from email import message_from_string
import re

from agentnews.services.email import build_report_email_html, markdown_to_email_html


def hrefs_in(html: str) -> list[str]:
    return re.findall(r'href="([^"]*)"', html)


MARKDOWN = """# 今日焦点
**重点**内容与 *说明*，以及 `代码片段`。

## 分类新闻

- 第一条 [原文](https://example.com/a)
- 第二条 <https://example.com/b>

> 引自来源的判断。

```python
print("hello")
```

| 主题 | 数量 |
| :--- | ---: |
| AI | 3 |
"""


def test_markdown_is_rendered_into_structured_html() -> None:
    html = markdown_to_email_html(MARKDOWN)

    assert "<h1" in html and "今日焦点" in html
    assert "<h2" in html and "分类新闻" in html
    assert "<strong" in html and "重点" in html
    assert "<em>" in html and "说明" in html
    assert "<code" in html and "代码片段" in html
    assert "<ul" in html and html.count("<li") == 2
    assert "<blockquote" in html
    assert '<pre style="' in html and "print" in html
    assert "<table" in html and "<th" in html
    # Alignment is preserved for right-aligned columns.
    assert "text-align:right;" in html


def test_links_are_restricted_to_http_and_hardened() -> None:
    html = markdown_to_email_html(
        "[正常](https://example.com/a) [脚本](javascript:alert(1)) "
        "[数据](data:text/html;base64,AA) [相对](/etc/passwd) https://example.com/plain。"
    )

    hrefs = re.findall(r'href="([^"]*)"', html)
    assert hrefs == ["https://example.com/a", "https://example.com/plain"]
    # Rejected targets are dropped, not printed: no scheme survives as text.
    assert "javascript:" not in html
    assert "data:text/html" not in html
    assert "/etc/passwd" not in html
    assert "脚本" in html and "数据" in html and "相对" in html
    assert 'rel="noopener noreferrer"' in html


def test_raw_html_in_report_content_stays_text() -> None:
    payload = "段落里含 <script>alert(1)</script> 与 <img src=x onerror=alert(2)>"
    html = markdown_to_email_html(payload)

    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "<img" not in html
    assert "onerror" in html  # present only as escaped text


def test_html_attributes_cannot_be_broken_out_of() -> None:
    # A quote inside the target must not survive into the markup as an attribute.
    html = markdown_to_email_html('[点击](https://example.com/a" onmouseover="alert(1))')

    assert "onmouseover" not in html
    assert hrefs_in(html) == []


def test_quoted_target_does_not_confuse_the_closing_parenthesis() -> None:
    html = markdown_to_email_html(
        '[坏](https://example.com/a") 后面还 [好](https://example.com/b)'
    )

    # The quoted target is rejected (it contains a quote), and crucially it does
    # not swallow the following link.
    hrefs = hrefs_in(html)
    assert hrefs == ["https://example.com/a", "https://example.com/b"]
    assert 'href="https://example.com/a"' in html


def test_links_with_balanced_parentheses_are_kept_whole() -> None:
    html = markdown_to_email_html("- [来源标题](https://example.com/x?q=(a)b)")

    assert 'href="https://example.com/x?q=(a)b"' in html


def test_angle_bracket_link_destinations_are_clickable() -> None:
    html = markdown_to_email_html("- [来源](<https://example.com/a>)")

    assert 'href="https://example.com/a"' in html
    assert "&lt;https" not in html


def test_empty_and_whitespace_markdown_render_without_blocks() -> None:
    assert markdown_to_email_html("").strip() == ""
    assert markdown_to_email_html("   \n\n  ").strip() == ""


def test_document_looks_like_a_styled_email() -> None:
    html = build_report_email_html(
        title="<日报标题>",
        report_date="2026-10-16",
        markdown="# 标题\n\n正文",
    )

    assert html.startswith("<!doctype html>")
    assert "&lt;日报标题&gt;" in html
    assert "<日报标题>" not in html
    assert "2026-10-16" in html
    assert "max-width:640px" in html
    assert html.rstrip().endswith("</html>")


def test_multipart_shape_is_what_the_tests_expect() -> None:
    """Guard the alternative structure that send_report relies on."""
    message = EmailMessage()
    message["From"] = "reports@example.test"
    message["To"] = "user@example.test"
    message["Subject"] = "AgentNews 日报 | 2026-10-16 | 测试日报"
    message.set_content("plain body")
    message.add_alternative("<p>html body</p>", subtype="html")

    assert message.is_multipart()
    parts = list(message.iter_parts())
    assert [part.get_content_type() for part in parts] == ["text/plain", "text/html"]
    assert "plain body" in parts[0].get_content()
    assert "html body" in parts[1].get_content()

    reparsed = message_from_string(message.as_string())
    assert [part.get_content_type() for part in reparsed.walk() if not part.is_multipart()] == [
        "text/plain",
        "text/html",
    ]
