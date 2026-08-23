from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-image.yml"
DOCKERFILE = ROOT / "Dockerfile"


def test_image_publication_only_runs_after_successful_main_ci() -> None:
    workflow = WORKFLOW.read_text()

    assert "workflow_run:" in workflow
    assert "workflows: [CI]" in workflow
    assert "branches: [main]" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "github.event.workflow_run.head_sha" in workflow
    assert "workflow_dispatch:" not in workflow


def test_image_publication_is_immutable_and_records_release_metadata() -> None:
    workflow = WORKFLOW.read_text()
    dockerfile = DOCKERFILE.read_text()

    assert "packages: write" in workflow
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert 'print(f"image=ghcr.io/{owner}/tradeops")' in workflow
    assert "^[0-9a-f]{40}$" in workflow
    assert "^sha256:[0-9a-f]{64}$" in workflow
    assert "SOURCE_SHA=${{ steps.release.outputs.source_sha }}" in workflow
    assert "VERSION=${{ steps.release.outputs.version }}" in workflow
    assert "SCHEMA_REVISION=${{ steps.release.outputs.schema_revision }}" in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "subject-digest: ${{ steps.build.outputs.digest }}" in workflow
    assert ":latest" not in workflow

    assert 'org.opencontainers.image.revision="${SOURCE_SHA}"' in dockerfile
    assert 'org.opencontainers.image.version="${VERSION}"' in dockerfile
    assert 'io.tradeops.source-sha="${SOURCE_SHA}"' in dockerfile
    assert 'io.tradeops.schema-revision="${SCHEMA_REVISION}"' in dockerfile


def test_publication_workflow_has_no_private_deployment_surface() -> None:
    workflow = WORKFLOW.read_text()

    forbidden = (
        "PRODUCTION_SSH",
        "ssh ",
        "known_hosts",
        "systemctl",
        "docker compose",
        "deploy-production",
    )
    assert not any(value in workflow for value in forbidden)
    assert not re.search(r"https?://(?!github\.com/)", workflow)


def test_all_actions_are_pinned_to_full_commit_shas() -> None:
    workflow = WORKFLOW.read_text()
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", value) for value in uses)
