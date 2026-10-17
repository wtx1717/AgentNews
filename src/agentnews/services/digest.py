from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..agent import AgentRunResult, AgentRunner, ToolCallRecord, create_agent_runner, get_prompt
from ..config import Settings, get_settings
from ..repositories import (
    ReportConflictError,
    RepositoryProtocol,
    ReportStorage,
    SourceArchiveStorage,
    StoredReport,
    StoredSourceArchive,
)


MAX_REPORT_TITLE_LENGTH = 120
MAX_UNCITED_SOURCES = 12
MAX_REPORT_SUMMARY_LENGTH = 300

_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+")
_BULLET_RE = re.compile(r"^ {0,3}[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^ {0,3}\d{1,3}[.)]\s+")
_SOURCE_LIST_HEADING_RE = re.compile(
    r"^#{2,6}\s*(?:来源|参考来源|参考资料|资料来源|信息来源|引用来源|引用列表)\s*[:：]?\s*$"
)
_PROCESS_HEADING_RE = re.compile(
    r"^#{2,6}\s*(?:工具证据|证据说明|检索说明|抓取说明|工具调用记录|过程说明|检索过程)\s*[:：]?\s*$"
)
_PROCESS_PARAGRAPH_RE = re.compile(
    r"^\*\*(?:证据|工具证据|检索|抓取|过程|验证)[^*]{0,8}\*\*\s*[:：]"
)
_OVERVIEW_HEADING_RE = re.compile(
    r"^#{2,6}\s*(?:今日要点|概览|摘要|要点速览|核心要点)\s*$"
)


class DigestError(RuntimeError):
    """Base class for one-off digest failures."""


class DigestConfigurationError(DigestError):
    """The supplied digest preferences are not usable."""


class DigestNoSourcesError(DigestError):
    """The agent did not produce a reliable source URL."""


class DigestEmptyResponseError(DigestError):
    """The agent did not produce report text."""


class DigestIncompleteRunError(DigestError):
    """The agent stopped before completing the digest task."""


class DigestStorageError(DigestError):
    """A report or source archive could not be stored safely."""


@dataclass(frozen=True, slots=True)
class DigestPreferences:
    """Preferences explicitly supplied to one digest run."""

    topics: tuple[str, ...] | Sequence[str] = ()
    keywords: tuple[str, ...] | Sequence[str] = ()
    timezone: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        object.__setattr__(self, "topics", _normalise_preferences(self.topics, "topics"))
        object.__setattr__(self, "keywords", _normalise_preferences(self.keywords, "keywords"))
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise DigestConfigurationError("日报时区不能为空")
        try:
            ZoneInfo(self.timezone.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise DigestConfigurationError("日报时区必须是有效的 IANA 时区") from None
        object.__setattr__(self, "timezone", self.timezone.strip())

    @property
    def has_topics(self) -> bool:
        return bool(self.topics or self.keywords)


@dataclass(frozen=True, slots=True)
class DigestResult:
    report_id: int
    report_path: str
    source_archive_paths: tuple[str, ...]
    tool_calls: int
    final_text: str
    title: str = ""
    summary: str = ""

    @property
    def answer(self) -> str:
        return self.final_text


@dataclass(slots=True)
class _SourceEvidence:
    url: str
    title: str = "未知来源"
    source_site: str = ""
    published_at: str | None = None
    snippet: str = ""
    body: str | None = None
    fetched_at: str | None = None


class DigestService:
    """Run and persist one preference-driven news digest."""

    def __init__(
        self,
        agent_runner: AgentRunner | Any | None = None,
        *,
        repository: RepositoryProtocol,
        settings: Settings | None = None,
        report_storage: ReportStorage | None = None,
        source_archive_storage: SourceArchiveStorage | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.agent_runner = agent_runner or create_agent_runner(settings=self.settings)
        self.repository = repository
        self.report_storage = report_storage or ReportStorage(self.settings.report_root)
        self.source_archive_storage = source_archive_storage or SourceArchiveStorage(
            self.settings.source_archive_root
        )

    async def run_once(
        self,
        preferences: DigestPreferences | Mapping[str, Any] | Any,
        business_date: date,
    ) -> DigestResult:
        prefs = _coerce_preferences(preferences)
        if not isinstance(business_date, date) or isinstance(business_date, datetime):
            raise DigestConfigurationError("业务日期必须是 date")
        if not prefs.has_topics:
            raise DigestConfigurationError("至少需要提供一个日报主题或关键词")

        task_context = _render_task_context(prefs, business_date)
        system_prompt = "\n\n".join(
            (
                get_prompt("digest_system"),
                get_prompt("web_research"),
                get_prompt("source_archive"),
                get_prompt("digest_markdown"),
            )
        )
        run = await self.agent_runner.ainvoke(
            "请根据本次任务上下文检索并撰写一份新闻日报。只返回日报 Markdown 正文，"
            "从一级标题开始，不要附加任何过程说明。",
            system_prompt=system_prompt,
            task_context=task_context,
        )
        if not isinstance(run, AgentRunResult):
            raise DigestError("Agent runner 返回了无效结果")
        if run.stop_reason != "completed":
            raise DigestIncompleteRunError("Agent 工具调用达到上限，日报任务未完成")
        if not run.text.strip():
            raise DigestEmptyResponseError("Agent 未返回日报正文")
        final_text = _sanitize_agent_report(run.text)
        if not final_text:
            raise DigestEmptyResponseError("Agent 未返回日报正文")

        all_sources = _extract_sources(run.tool_call_records)
        if not all_sources:
            raise DigestNoSourcesError("没有可靠来源，无法生成日报")
        sources = _select_report_sources(all_sources, final_text)

        title = _extract_report_title(final_text) or _fallback_report_title(prefs)
        summary = _report_summary(final_text)
        reservation = self.repository.reserve_report(business_date, title)
        report_id = reservation.id
        stored_report = None
        stored_sources: list[StoredSourceArchive] = []
        archive_paths: list[str] = []
        indexed_sources: list[tuple[_SourceEvidence, str | None]] = []
        try:
            report_content = _render_report(title, final_text, sources)
            stored_report = self.report_storage.save_report(
                report_id, business_date, title, report_content
            )
            for index, source in enumerate(sources, start=1):
                archive_path = None
                if source.body is not None:
                    stored_source = self.source_archive_storage.save_source(
                        report_id,
                        index,
                        business_date,
                        source.title,
                        source.url,
                        source.source_site,
                        _normalise_fetched_at(source.fetched_at),
                        source.published_at,
                        source.body,
                    )
                    stored_sources.append(stored_source)
                    archive_path = stored_source.relative_path
                    archive_paths.append(archive_path)
                indexed_sources.append((source, archive_path))
            for source, archive_path in indexed_sources:
                self.repository.add_report_source(
                    report_id,
                    title=source.title,
                    url=source.url,
                    source_name=source.source_site,
                    published_at=source.published_at,
                    snippet=source.snippet,
                    archive_file_path=archive_path,
                )
            self.repository.finalize_report(
                report_id,
                file_path=stored_report.relative_path,
                summary=summary,
                content_hash=stored_report.content_hash,
            )
        except ReportConflictError as error:
            _rollback_created_files(self, stored_report, stored_sources)
            _cancel_report(self.repository, report_id)
            raise DigestStorageError("日报或来源归档文件已存在且内容不同，未覆盖原文件") from error
        except Exception as error:
            _rollback_created_files(self, stored_report, stored_sources)
            _cancel_report(self.repository, report_id)
            raise DigestStorageError("日报或来源归档保存失败") from error

        return DigestResult(
            report_id=report_id,
            report_path=stored_report.relative_path,
            source_archive_paths=tuple(archive_paths),
            tool_calls=run.tool_calls,
            final_text=final_text,
            title=title,
            summary=summary,
        )


def _normalise_preferences(values: Sequence[str] | Any, field_name: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise DigestConfigurationError(f"{field_name} 必须是字符串列表")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise DigestConfigurationError(f"{field_name} 必须只包含字符串")
        value = value.strip()
        if value and value not in result:
            result.append(value)
    return tuple(result)


def _rollback_created_files(
    service: DigestService,
    report: StoredReport | None,
    sources: Sequence[StoredSourceArchive],
) -> None:
    for source in reversed(sources):
        try:
            service.source_archive_storage.discard_created_source(source)
        except (OSError, ValueError):
            pass
    if report is not None:
        try:
            service.report_storage.discard_created_report(report)
        except (OSError, ValueError):
            pass


def _coerce_preferences(value: DigestPreferences | Mapping[str, Any] | Any) -> DigestPreferences:
    if isinstance(value, DigestPreferences):
        return value
    if isinstance(value, Mapping):
        return DigestPreferences(
            topics=value.get("topics", ()),
            keywords=value.get("keywords", ()),
            timezone=value.get("timezone", "Asia/Shanghai"),
        )
    try:
        return DigestPreferences(
            topics=getattr(value, "topics"),
            keywords=getattr(value, "keywords"),
            timezone=getattr(value, "timezone"),
        )
    except AttributeError:
        raise DigestConfigurationError("日报偏好对象无效") from None


def _render_task_context(preferences: DigestPreferences, business_date: date) -> str:
    topics = "、".join(preferences.topics) or "无"
    keywords = "、".join(preferences.keywords) or "无"
    weekday = "星期" + "一二三四五六日"[business_date.weekday()]
    window_start = business_date - timedelta(days=6)
    priority_start = business_date - timedelta(days=2)
    return (
        f"业务日期：{business_date.isoformat()}（{weekday}）\n"
        f"时区：{preferences.timezone}\n"
        f"检索窗口：{window_start.isoformat()} 至 {business_date.isoformat()}"
        f"（优先 {priority_start.isoformat()} 至 {business_date.isoformat()}）\n"
        f"关注主题：{topics}\n"
        f"关键词：{keywords}\n"
        "报告语言：简体中文"
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalise_fetched_at(value: str | None) -> str:
    if not value:
        return _utc_now()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _utc_now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _extract_report_title(text: str) -> str:
    """Return the first level-one Markdown heading outside code fences."""
    for line in _lines_outside_fences(text):
        if not re.match(r"^ {0,3}#\s+", line):
            continue
        title = _clean_report_title(line.strip()[1:].strip())
        if title:
            return title
    return ""


def _lines_outside_fences(text: str) -> list[str]:
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)
    return lines


def _clean_report_title(value: str) -> str:
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cleaned)
    cleaned = re.sub(r"[`*_~]+", "", cleaned)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = "".join(character for character in cleaned if character.isprintable())
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.strip(" #\t-")
    return cleaned[:MAX_REPORT_TITLE_LENGTH].rstrip()


def _fallback_report_title(preferences: DigestPreferences) -> str:
    subjects = preferences.topics or preferences.keywords or ("新闻",)
    return _clean_report_title(f"{subjects[0]} 日报")


def _plain_inline(value: str) -> str:
    """Strip inline Markdown so summaries stay readable as plain text."""
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cleaned)
    cleaned = re.sub(r"[`*_~]+", "", cleaned)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return " ".join(cleaned.split()).strip()


def _sanitize_agent_report(text: str) -> str:
    """Remove model narration and duplicated meta sections from report body."""
    if not isinstance(text, str):
        return ""
    value = text.strip()
    if not value:
        return ""
    value = _strip_agent_preamble(value)
    value = _strip_process_sections(value)
    value = _normalise_markdown_whitespace(value)
    return value.strip()


def _strip_agent_preamble(text: str) -> str:
    lines = text.splitlines()
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not re.match(r"^ {0,3}#\s+", line):
            continue
        if not stripped[1:].strip():
            return text
        return "\n".join(lines[index:]).strip() if index else text
    return text


def _strip_process_sections(text: str) -> str:
    lines = text.splitlines()
    changed = True
    while changed:
        changed = False
        while lines and not lines[-1].strip():
            lines = lines[:-1]
        if not lines:
            break
        heading_index = _last_heading_index(lines)
        if heading_index is not None:
            heading = lines[heading_index].strip()
            if _SOURCE_LIST_HEADING_RE.match(heading) and _is_link_list(
                lines[heading_index + 1 :]
            ):
                lines = lines[:heading_index]
                changed = True
                continue
            if _PROCESS_HEADING_RE.match(heading):
                lines = lines[:heading_index]
                changed = True
                continue
        if _PROCESS_PARAGRAPH_RE.match(lines[-1].strip()):
            cut = len(lines) - 1
            while cut > 0 and lines[cut - 1].strip():
                cut -= 1
            lines = lines[:cut]
            changed = True
    return "\n".join(lines).strip()


def _last_heading_index(lines: Sequence[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if _HEADING_RE.match(lines[index]):
            return index
    return None


def _is_link_list(lines: Sequence[str]) -> bool:
    seen_item = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if not (_BULLET_RE.match(line) or _ORDERED_RE.match(line)):
            return False
        seen_item = True
    return seen_item


def _normalise_markdown_whitespace(text: str) -> str:
    lines = text.splitlines()
    result: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() == "---":
            previous_blank = not result or not result[-1].strip()
            next_blank = index + 1 >= len(lines) or not lines[index + 1].strip()
            if previous_blank and next_blank:
                continue
        result.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(result)).strip()


def _select_report_sources(
    sources: Sequence[_SourceEvidence], text: str
) -> list[_SourceEvidence]:
    """Keep cited sources first, then fall back to sources whose body was fetched."""
    positioned: list[tuple[int, _SourceEvidence]] = []
    for source in sources:
        position = _citation_position(source.url, text)
        if position >= 0:
            positioned.append((position, source))
    if positioned:
        positioned.sort(key=lambda item: item[0])
        return [source for _, source in positioned]

    fetched = [source for source in sources if source.body is not None]
    if fetched:
        return fetched[:MAX_UNCITED_SOURCES]
    return list(sources[:MAX_UNCITED_SOURCES])


def _citation_position(url: str, text: str) -> int:
    positions = [text.find(variant) for variant in _url_variants(url) if variant]
    found = [position for position in positions if position >= 0]
    return min(found) if found else -1


def _url_variants(url: str) -> set[str]:
    variants = set()
    for candidate in (url, url.rstrip("/"), unquote(url), unquote(url.rstrip("/"))):
        if candidate:
            variants.add(candidate)
            variants.add(candidate.replace("(", "%28").replace(")", "%29"))
    return variants


def _markdown_link_destination(url: str) -> str:
    """Escape characters the narrow Markdown renderers would reject."""
    return (
        url.replace("(", "%28")
        .replace(")", "%29")
        .replace("<", "%3C")
        .replace(">", "%3E")
    )


def _render_report(title: str, text: str, sources: Sequence[_SourceEvidence]) -> str:
    body = text.rstrip()
    if not _extract_report_title(body):
        body = f"# {title}\n\n{body}"
    lines = [body, "", "## 参考来源", ""]
    for source in sources:
        title_text = source.title or "未知来源"
        safe_title = title_text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        destination = _markdown_link_destination(source.url)
        meta = "，".join(
            part
            for part in (
                (source.source_site or "").strip(),
                (source.published_at or "").strip(),
            )
            if part
        )
        suffix = f"（{meta}）" if meta else ""
        lines.append(f"- [{safe_title}]({destination}){suffix}")
    return "\n".join(lines) + "\n"


def _report_summary(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _OVERVIEW_HEADING_RE.match(line.strip()):
            continue
        bullets: list[str] = []
        for child in lines[index + 1 :]:
            if _HEADING_RE.match(child):
                break
            match = _BULLET_RE.match(child)
            if match:
                cleaned = _plain_inline(match.group(1))
                if cleaned:
                    bullets.append(cleaned)
        if bullets:
            return "；".join(bullets[:3])[:MAX_REPORT_SUMMARY_LENGTH]
        break

    for line in lines:
        stripped = line.strip()
        if not stripped or _HEADING_RE.match(line):
            continue
        if stripped.startswith((">", "|", "```", "~~~")):
            continue
        if _BULLET_RE.match(line) or _ORDERED_RE.match(line):
            continue
        if _PROCESS_PARAGRAPH_RE.match(stripped):
            continue
        cleaned = _plain_inline(stripped)
        if cleaned:
            return cleaned[:MAX_REPORT_SUMMARY_LENGTH]
    return ""


def _cancel_report(repository: RepositoryProtocol, report_id: int) -> None:
    try:
        repository.cancel_report(report_id)
    except Exception:
        pass


def _extract_sources(records: Sequence[ToolCallRecord]) -> list[_SourceEvidence]:
    by_url: dict[str, _SourceEvidence] = {}
    for record in records:
        tool_name = record.name.lower()
        search_like = _is_search_tool(tool_name)
        fetch_like = _is_fetch_tool(tool_name)
        arguments = record.arguments if isinstance(record.arguments, Mapping) else {}
        fallback_url = _first_text(arguments, "url", "uri", "link", "source_url")
        fallback_title = _first_text(arguments, "title", "name")
        for mapping in _iter_mappings(record.result):
            url = _first_text(mapping, "url", "source_url", "original_url", "link") or fallback_url
            if not _is_http_url(url):
                continue
            title = _first_text(mapping, "title", "headline", "name") or fallback_title or "未知来源"
            source_site = (
                _first_text(mapping, "source_site", "source_name", "domain", "site")
                or (urlsplit(url).hostname or "")
            )
            published_at = _first_text(mapping, "published_at", "published_date", "published_time", "date")
            fetched_at = _first_text(mapping, "fetched_at", "retrieved_at", "crawled_at", "scraped_at")
            snippet_names = ("snippet", "summary", "description", "content", "raw_content") if search_like else (
                "snippet", "summary", "description"
            )
            snippet = _first_text(mapping, *snippet_names)
            content = _body_text(mapping) if fetch_like else ""
            has_search_evidence = search_like and bool(snippet)
            has_fetched_evidence = fetch_like and bool(content)
            if not (has_search_evidence or has_fetched_evidence):
                continue
            source = by_url.get(url)
            if source is None:
                source = _SourceEvidence(url=url, title=title, source_site=source_site)
                by_url[url] = source
            if source.title == "未知来源" and title != "未知来源":
                source.title = title
            if not source.source_site:
                source.source_site = source_site
            if published_at and not source.published_at:
                source.published_at = published_at
            if fetched_at and not source.fetched_at:
                source.fetched_at = fetched_at
            if snippet and not source.snippet:
                source.snippet = snippet
            if content and source.body is None:
                source.body = content
    return list(by_url.values())


def _is_search_tool(name: str) -> bool:
    return "search" in name or name in {"web_search", "tavily"}


def _is_fetch_tool(name: str) -> bool:
    return any(
        marker in name
        for marker in (
            "fetch", "extract", "browser", "scrape", "crawl", "webpage", "page_content",
            "read_url", "open_url", "get_url",
        )
    )


def _iter_mappings(value: Any) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return _iter_mappings(json.loads(stripped))
            except json.JSONDecodeError:
                return []
        return []
    if isinstance(value, Mapping):
        result.append(value)
        for child in value.values():
            result.extend(_iter_mappings(child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            result.extend(_iter_mappings(child))
    return result


def _first_text(mapping: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = mapping.get(name)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _body_text(mapping: Mapping[str, Any]) -> str:
    content_type = _first_text(mapping, "content_type", "contentType", "mime_type", "mimeType")
    content_type_lower = content_type.lower()
    if content_type_lower:
        mime_type = content_type_lower.split(";", maxsplit=1)[0].strip()
        if not mime_type.startswith("text/") or mime_type in {"text/html", "text/xhtml"}:
            return ""
    for name in ("body", "text", "markdown", "article_text", "raw_text", "raw_content", "content"):
        value = mapping.get(name)
        if name == "html":
            continue
        if isinstance(value, str):
            candidate = value.strip()
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            parts = []
            for item in value:
                if isinstance(item, Mapping):
                    text = _first_text(item, "text", "content")
                    if text:
                        parts.append(text)
            candidate = "\n".join(parts).strip()
        else:
            candidate = ""
        if candidate and "\x00" not in candidate and not _looks_like_html(candidate):
            return candidate
    return ""


def _looks_like_html(value: str) -> bool:
    stripped = value.lstrip().lower()
    if re.match(
        r"^<(?:!doctype\s+html|/?(?:html|head|body|article|main|div|p|h[1-6]|section|script|style|ul|ol|li|span|table|title|meta))\b",
        stripped,
    ):
        return True
    return len(re.findall(r"</?[a-z][^>]*>", value, flags=re.IGNORECASE)) >= 3


def _is_http_url(value: str) -> bool:
    if any(character.isspace() or ord(character) < 32 for character in value) or "<" in value or ">" in value:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
