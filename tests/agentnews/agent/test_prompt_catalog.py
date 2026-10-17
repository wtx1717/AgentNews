from __future__ import annotations

import pytest

from agentnews.agent import PromptNotFoundError, get_prompt


PROMPT_SCENES = (
    "interaction",
    "web_research",
    "digest_system",
    "digest_markdown",
    "source_archive",
)


@pytest.mark.parametrize("scene", PROMPT_SCENES)
def test_prompt_scenes_load_default_and_explicit_v2(scene):
    assert get_prompt(scene) == get_prompt(scene, "v2")


@pytest.mark.parametrize("scene", PROMPT_SCENES)
def test_every_scene_keeps_v1_loadable_for_rollback(scene):
    legacy = get_prompt(scene, "v1")
    assert legacy
    assert legacy != get_prompt(scene, "v2")


def test_unknown_prompt_scene_raises_clear_error():
    with pytest.raises(PromptNotFoundError, match="未知提示词场景"):
        get_prompt("unknown")


def test_unknown_prompt_version_raises_clear_error():
    with pytest.raises(PromptNotFoundError, match="未注册版本"):
        get_prompt("interaction", "v9")
    with pytest.raises(PromptNotFoundError, match="未注册版本"):
        get_prompt("interaction", "")


def test_prompts_define_tool_search_write_and_reliability_rules():
    interaction = get_prompt("interaction")
    research = get_prompt("web_research")
    digest = get_prompt("digest_system")
    archive = get_prompt("source_archive")

    for prompt in (interaction, research, digest, archive):
        assert "已注册" in prompt
        assert "bash" in prompt

    assert "何时搜索" in research
    assert "何时抓取正文" in research
    assert "仅当" in research
    assert "信息不足" in research
    assert "写入文件" in interaction
    assert "先向用户确认" in interaction
    assert "前端审核弹窗是唯一的执行确认步骤" in interaction
    assert "不要先询问“是否执行”" in interaction
    assert "agentnews reports delete --id" in interaction
    assert "不要用 `Remove-Item`" in interaction
    assert "不得调用 Bash、PowerShell" in interaction
    assert "DigestService" in interaction
    assert "agentnews reports generate" in interaction
    assert "先给结论" in interaction
    assert "不要复述整份日报" in interaction
    assert "真实来源 URL" in digest
    assert "不得调用任何文件写入、覆盖或删除工具" in digest
    assert "只返回 Markdown 正文" in digest
    markdown = get_prompt("digest_markdown")
    assert "真实 URL" in markdown
    assert "一级标题" in markdown
    assert "空泛标题" in markdown
    assert "参考来源" in markdown
    assert "今日要点" in markdown
    assert "原始 URL" in archive
    assert "不得猜测、改写或伪造" in archive
    assert "文件写入" in archive


def test_interaction_prompt_treats_bash_as_a_general_fallback():
    interaction = get_prompt("interaction")

    assert "专用工具优先" in interaction
    assert "终端兜底" in interaction
    assert "用户不需要先提到 Bash" in interaction
    assert "不要在没有尝试工具之前声称" in interaction
    assert "下载" in interaction
    assert "Invoke-WebRequest" in interaction
    assert "data/downloads" in interaction
    assert "文件工具只处理文本" in interaction
    assert "Get-Item" in interaction


def test_prompt_files_are_loaded_as_package_resources():
    assert "交互式信息助理" in get_prompt("interaction")
    assert "一次性新闻日报任务" in get_prompt("digest_system")
