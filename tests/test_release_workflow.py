from pathlib import Path


WORKFLOW = Path(".github/workflows/release.yml")


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
