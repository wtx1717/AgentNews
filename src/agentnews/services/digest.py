from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
import json
import re
from typing import Any
from urllib.parse import urlsplit
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
                get_prompt("digest_markdown"),
                get_prompt("source_archive"),
                "本次任务由日报服务负责保存日报与来源归档。请只返回日报 Markdown 和实际工具证据，不要调用任何文件写入工具。",
            )
        )
        run = await self.agent_runner.ainvoke(
            "请根据本次任务上下文生成一次性新闻日报，并保留真实来源 URL。",
            system_prompt=system_prompt,
            task_context=task_context,
        )
        if not isinstance(run, AgentRunResult):
            raise DigestError("Agent runner 返回了无效结果")
        if run.stop_reason != "completed":
            raise DigestIncompleteRunError("Agent 工具调用达到上限，日报任务未完成")
        final_text = run.text.strip()
        if not final_text:
            raise DigestEmptyResponseError("Agent 未返回日报正文")

        sources = _extract_sources(run.tool_call_records)
        if not sources:
            raise DigestNoSourcesError("没有可靠来源，无法生成日报")

        report_content = _render_report(final_text, sources)
        title = f"{business_date.isoformat()} 新闻日报"
        reservation = self.repository.reserve_report(business_date, title)
        report_id = reservation.id
        stored_report = None
        stored_sources = []
        try:
            stored_report = self.report_storage.save_report(
                report_id, business_date, title, report_content
            )
            archive_paths: list[str] = []
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
                summary=_report_summary(final_text),
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
    return (
        f"业务日期：{business_date.isoformat()}\n"
        f"时区：{preferences.timezone}\n"
        f"关注主题：{topics}\n"
        f"关键词：{keywords}"
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


def _render_report(text: str, sources: Sequence[_SourceEvidence]) -> str:
    lines = [text.rstrip(), "", "## 来源", ""]
    for source in sources:
        title = source.title or "未知来源"
        safe_title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        lines.append(f"- [{safe_title}](<{source.url}>)")
    return "\n".join(lines) + "\n"


def _report_summary(text: str) -> str:
    paragraphs = [" ".join(part.split()) for part in text.split("\n\n") if part.strip()]
    return paragraphs[0][:300] if paragraphs else ""


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
