"""Versioned prompt resources for AgentNews scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Mapping


class PromptNotFoundError(LookupError):
    """Raised when a prompt scenario or version is not registered."""


@dataclass(frozen=True, slots=True)
class _PromptScenario:
    default_version: str
    resources: Mapping[str, str]


_PROMPTS: Mapping[str, _PromptScenario] = MappingProxyType(
    {
        "interaction": _PromptScenario(
            default_version="v1",
            resources=MappingProxyType({"v1": "interaction/v1.md"}),
        ),
        "web_research": _PromptScenario(
            default_version="v1",
            resources=MappingProxyType({"v1": "web_research/v1.md"}),
        ),
        "digest_system": _PromptScenario(
            default_version="v1",
            resources=MappingProxyType({"v1": "digest_system/v1.md"}),
        ),
        "digest_markdown": _PromptScenario(
            default_version="v1",
            resources=MappingProxyType({"v1": "digest_markdown/v1.md"}),
        ),
        "source_archive": _PromptScenario(
            default_version="v1",
            resources=MappingProxyType({"v1": "source_archive/v1.md"}),
        ),
    }
)


def get_prompt(scene: str, version: str | None = None) -> str:
    """Load a registered prompt, using its default version when omitted."""
    scenario = _PROMPTS.get(scene)
    if scenario is None:
        raise PromptNotFoundError(f"未知提示词场景：{scene}")

    selected_version = scenario.default_version if version is None else version
    resource = scenario.resources.get(selected_version)
    if resource is None:
        raise PromptNotFoundError(
            f"提示词场景 {scene!r} 未注册版本 {selected_version!r}"
        )

    resource_path = files(__package__).joinpath("prompts", *resource.split("/"))
    try:
        return resource_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise RuntimeError(
            f"提示词资源缺失：场景 {scene!r}，版本 {selected_version!r}"
        ) from error
