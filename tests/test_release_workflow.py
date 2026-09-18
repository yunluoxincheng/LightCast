import re
from pathlib import Path


WORKFLOW = Path(".github/workflows/release.yml")

# 评审 M17：浮动版本 tag（如 @v5）可被重定向利用，所有 uses 引用必须
# 钉死完整 40 位 commit SHA；已知的具体 action 名在此锁定，防止删掉
# 现有 action 后测试因集合为空而静默通过。
_EXPECTED_ACTIONS = {
    "actions/checkout",
    "actions/setup-python",
    "actions/upload-artifact",
}


def test_remote_state_is_read_inside_serialized_build_job() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    metadata_start = workflow.index("  metadata:")
    build_start = workflow.index("  build:")
    concurrency_start = workflow.index("    concurrency:", build_start)
    state_step = workflow.index("      - name: Resolve current remote release state")

    assert "gh api" not in workflow[metadata_start:build_start]
    assert state_step > concurrency_start


def test_publish_paths_verify_existing_tag_and_recover_drafts() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    verify_step = workflow.index("Verify release tag target")
    create_release = workflow.index("Publish new or missing release")
    recover_draft = workflow.index("Recover and publish draft release")

    assert "gh release upload \"$tag\"" in workflow
    assert "--clobber" in workflow
    assert "--draft=false" in workflow
    assert 'releases/$releaseId' in workflow
    assert "Release 已不再是预期 draft" in workflow
    assert workflow.count("--verify-tag") >= 2
    assert verify_step < create_release
    assert verify_step < recover_draft


def test_all_actions_are_pinned_to_full_commit_sha() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    refs = re.findall(r"uses:\s*(\S+)", workflow)

    assert refs, "workflow 中未找到任何 uses 引用"
    actions = set()
    for ref in refs:
        action, _, version = ref.partition("@")
        assert action and version, f"uses 引用缺少版本: {ref}"
        assert re.fullmatch(r"[0-9a-f]{40}", version), (
            f"{ref} 未钉住完整 commit SHA（当前版本段: {version}），"
            "浮动 tag 可被重定向利用"
        )
        actions.add(action)

    assert actions == _EXPECTED_ACTIONS, (
        f"workflow 的 action 集合变化: {sorted(actions)}；"
        "新增 action 必须同样钉 SHA 并更新 _EXPECTED_ACTIONS"
    )


def test_libmpv_download_requires_x86_64_and_validates_pe_machine() -> None:
    """libmpv 下载必须限定 x86_64 且解压后校验 PE 架构。

    0.1.29~0.1.32 曾因 RSS 里 i686 构建更新被「仅排除 v3」的逻辑误选，
    安装包内置 32 位 dll，用户机器上启动即「libmpv 不可用」。
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    section = workflow[workflow.index("Download libmpv-2.dll"):]

    assert "$title -like '*x86_64*'" in section, "RSS 选择必须限定 x86_64"
    assert "未找到 x86_64 的 .7z 包" in section
    assert "0x8664" in section, "解压后必须校验 PE machine=0x8664"
    assert "不是 x64 PE" in section, "非 x64 构建必须使构建失败"
