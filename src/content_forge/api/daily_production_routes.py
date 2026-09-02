"""PR35 daily mobile attention projection over existing production authorities."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from content_forge.application import (
    AuthManager,
    AuthenticationError,
    AuthSession,
    InboxService,
)
from content_forge.application.production_presets import (
    ProductionPresetConflictError,
    ProductionPresetService,
    _preset_evidence,
    _source_snapshots,
)
from content_forge.application.review import ReviewError, ReviewService
from content_forge.providers import PublishingProvider
from content_forge.storage import LocalLibrary, StorageConflictError

from .project_publishing_routes import _attempt_payload, _project_attempt_ids

_GROUP_ORDER = ("failed", "attention", "safe_work", "working", "inbox", "finished")
_RENDER_OPERATION_PRIORITY = {"render_final": 0, "render_preview": 1}


class SafeWorkRequest(BaseModel):
    """Bound the synchronous compute one phone action may request."""

    model_config = ConfigDict(extra="forbid")
    render_limit: int = Field(default=4, ge=1, le=12)


def _authorization_token(value: str | None) -> str:
    if value is None or not value.startswith("Bearer "):
        raise AuthenticationError("bearer token required")
    token = value[7:].strip()
    if not token:
        raise AuthenticationError("bearer token required")
    return token


def _open_tasks(summary: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    raw = summary.get("tasks")
    if not isinstance(raw, list):
        return ()
    return tuple(
        item
        for item in raw
        if isinstance(item, Mapping) and item.get("status") == "open"
    )


def _blocking_tasks(summary: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return tuple(item for item in _open_tasks(summary) if item.get("blocking") is True)


def _preview_safe(summary: Mapping[str, object]) -> bool:
    if summary.get("state") != "needs_review":
        return False
    blockers = _blocking_tasks(summary)
    if len(blockers) != 1 or blockers[0].get("task_type") != "preview_approval":
        return False
    preview = summary.get("preview")
    return isinstance(preview, Mapping) and preview.get("status") == "not_rendered"


def _used_source_project_ids(projects: tuple[object, ...]) -> set[str]:
    used: set[str] = set()
    for project in projects:
        try:
            evidence = _preset_evidence(project)  # type: ignore[arg-type]
            if evidence is None:
                continue
            snapshots = _source_snapshots(evidence)
        except (ProductionPresetConflictError, TypeError, ValueError):
            continue
        for snapshot in snapshots:
            source_project_id = snapshot.get("source_project_id")
            if isinstance(source_project_id, str):
                used.add(source_project_id)
    return used


def _project_publish_projection(
    library: LocalLibrary,
    summary: Mapping[str, object],
) -> tuple[str | None, dict[str, object] | None]:
    final = summary.get("final")
    project_id = summary.get("project_id")
    if not isinstance(final, Mapping) or not isinstance(project_id, str):
        return None, None
    job_id = final.get("job_id")
    output_sha256 = final.get("output_sha256")
    if not isinstance(job_id, str) or not isinstance(output_sha256, str):
        raise StorageConflictError("DONE project has incomplete final identity")
    attempt_ids = _project_attempt_ids(
        library,
        project_id,
        job_id,
        output_sha256,
        limit=100,
    )
    if not attempt_ids:
        return None, None
    payloads = [_attempt_payload(library, attempt_id) for attempt_id in attempt_ids]
    states = [
        str(item.get("attempt", {}).get("state", ""))
        for item in payloads
        if isinstance(item.get("attempt"), Mapping)
    ]
    if states.count("outcome_unknown") > 0:
        return "outcome_unknown", payloads[0]
    if states.count("running") > 1 or states.count("prepared") > 1:
        raise StorageConflictError("exact final has multiple active publish attempts")
    if not states:
        raise StorageConflictError("exact final publishing attempt has no state")
    return states[0], payloads[0]


def _project_card(
    summary: Mapping[str, object],
    *,
    ready_ids: set[str],
    library: LocalLibrary,
) -> dict[str, object]:
    project_id = summary.get("project_id")
    state = summary.get("state")
    if not isinstance(project_id, str) or not isinstance(state, str):
        raise ValueError("project summary identity is malformed")

    group = "failed"
    reason = "Project state needs inspection"
    safe_operation: str | None = None
    publish_state: str | None = None

    if state == "inbox" or summary.get("review_initialized") is not True:
        group = "safe_work"
        reason = "Project can be prepared automatically"
        safe_operation = "bootstrap"
    elif state == "needs_review":
        blockers = _blocking_tasks(summary)
        preview = summary.get("preview")
        if any(item.get("attention") == "manual" for item in blockers):
            group = "attention"
            reason = "Manual setup is required"
        elif any(item.get("task_type") != "preview_approval" for item in blockers):
            group = "attention"
            reason = "A human decision is blocking preview"
        elif _preview_safe(summary):
            group = "safe_work"
            reason = "Preview can render automatically"
            safe_operation = "render_preview"
        elif isinstance(preview, Mapping) and preview.get("status") == "rendering":
            group = "working"
            reason = "Preview is rendering"
        elif isinstance(preview, Mapping) and preview.get("status") == "ready":
            group = "attention"
            reason = "Preview is ready for approval"
        elif blockers:
            group = "attention"
            reason = "Review is required"
        else:
            group = "failed"
            reason = "Review state has no actionable preview authority"
    elif state == "ready":
        if project_id in ready_ids:
            group = "safe_work"
            reason = "Approved project can render final automatically"
            safe_operation = "render_final"
        else:
            group = "failed"
            reason = "READY project is missing current final-render authority"
    elif state in {"rendering", "qc"}:
        group = "working"
        reason = "Final render or QC is in progress"
    elif state == "done":
        try:
            publish_state, _payload = _project_publish_projection(library, summary)
        except (StorageConflictError, TypeError, ValueError):
            group = "failed"
            reason = "Publishing history for this final needs inspection"
        else:
            if publish_state == "outcome_unknown":
                group = "failed"
                reason = "Remote publishing outcome is unknown"
            elif publish_state == "running":
                group = "working"
                reason = "Publishing may be in progress"
            elif publish_state == "prepared":
                group = "attention"
                reason = "Approved publish request awaits explicit execution"
            elif publish_state == "failed":
                group = "failed"
                reason = "Previous publish attempt failed safely"
            else:
                group = "finished"
                reason = (
                    "Published successfully"
                    if publish_state == "succeeded"
                    else "Final is complete"
                )

    return {
        "kind": "project",
        "group": group,
        "reason": reason,
        "safe_operation": safe_operation,
        "publish_state": publish_state,
        "project": dict(summary),
    }


def _safe_intake_payload(intake: object) -> dict[str, object]:
    raw = intake.model_dump(mode="json")  # type: ignore[attr-defined]
    keys = (
        "intake_id",
        "kind",
        "state",
        "project_id",
        "asset_id",
        "filename",
        "source_url",
        "creator_hint",
        "content_kind_hint",
        "error_code",
        "created_at",
        "updated_at",
    )
    return {key: raw.get(key) for key in keys if key in raw}


def install_daily_production_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    library: LocalLibrary,
    inbox: InboxService,
    review: ReviewService,
    presets: ProductionPresetService,
    provider: PublishingProvider | None,
) -> None:
    """Install PR35 projections without creating a second workflow authority."""

    def bearer_token(authorization: str | None = Header(default=None)) -> str:
        try:
            return _authorization_token(authorization)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def require_session(token: str = Depends(bearer_token)) -> AuthSession:
        try:
            return auth.authenticate(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def snapshot(limit: int) -> dict[str, object]:
        production_projects = presets.list_projects(limit=min(500, max(limit * 4, 100)))
        production_by_id = {project.project_id: project for project in production_projects}
        source_items = presets.list_sources(limit=500)
        source_ids = {str(item["source_project_id"]) for item in source_items}
        used_source_ids = _used_source_project_ids(production_projects)

        queue = review.list_queue(limit=500, include_auto=False)
        queue_items = queue.get("items")
        ready_raw = queue.get("ready_projects")
        ready_ids = {
            item
            for item in (ready_raw if isinstance(ready_raw, list) else [])
            if isinstance(item, str)
        }

        summaries: dict[str, Mapping[str, object]] = {}
        if isinstance(queue_items, list):
            for item in queue_items:
                if not isinstance(item, Mapping):
                    continue
                project_id = item.get("project_id")
                if isinstance(project_id, str) and project_id not in source_ids:
                    summaries[project_id] = item
        for project_id, project in production_by_id.items():
            summaries[project_id] = review.project_summary(project)
        for project_id in ready_ids:
            if project_id in source_ids or project_id in summaries:
                continue
            try:
                summaries[project_id] = review.project_summary(review.get_project(project_id))
            except ReviewError:
                continue

        cards: list[dict[str, object]] = []
        for summary in summaries.values():
            try:
                cards.append(_project_card(summary, ready_ids=ready_ids, library=library))
            except (StorageConflictError, TypeError, ValueError):
                project_id = summary.get("project_id")
                cards.append(
                    {
                        "kind": "project",
                        "group": "failed",
                        "reason": "Project summary is inconsistent",
                        "safe_operation": None,
                        "publish_state": None,
                        "project": {"project_id": project_id} if isinstance(project_id, str) else {},
                    }
                )

        for source in source_items:
            source_project_id = str(source["source_project_id"])
            if source_project_id in used_source_ids:
                continue
            cards.append(
                {
                    "kind": "source",
                    "group": "inbox",
                    "reason": "Unused source is ready for Create video",
                    "safe_operation": None,
                    "source": source,
                }
            )

        intakes = inbox.list_intakes(limit=500)
        represented_projects = set(summaries) | source_ids
        for intake_item in intakes:
            raw = _safe_intake_payload(intake_item)
            state = raw.get("state")
            project_id = raw.get("project_id")
            if state == "failed":
                cards.append(
                    {
                        "kind": "intake",
                        "group": "failed",
                        "reason": "Capture failed before production",
                        "safe_operation": None,
                        "intake": raw,
                    }
                )
            elif isinstance(project_id, str) and project_id not in represented_projects:
                cards.append(
                    {
                        "kind": "intake",
                        "group": "attention",
                        "reason": "Captured item needs project setup",
                        "safe_operation": None,
                        "intake": raw,
                    }
                )
                represented_projects.add(project_id)

        cards.sort(
            key=lambda item: (
                _GROUP_ORDER.index(str(item.get("group")))
                if item.get("group") in _GROUP_ORDER
                else len(_GROUP_ORDER),
                str(
                    item.get("project", {}).get("project_id", "")
                    if isinstance(item.get("project"), Mapping)
                    else item.get("source", {}).get("source_project_id", "")
                    if isinstance(item.get("source"), Mapping)
                    else item.get("intake", {}).get("intake_id", "")
                    if isinstance(item.get("intake"), Mapping)
                    else ""
                ),
            )
        )
        bounded = cards[:limit]
        counts = {
            group: sum(1 for item in cards if item.get("group") == group)
            for group in _GROUP_ORDER
        }
        return {
            "items": bounded,
            "counts": counts,
            "total": len(cards),
            "truncated": len(cards) > len(bounded),
            "provider_configured": provider is not None,
        }

    @app.get("/api/v1/production/attention")
    def attention_queue(
        limit: int = Query(default=100, ge=1, le=500),
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        return snapshot(limit)

    @app.post("/api/v1/production/safe-work")
    def run_safe_work(
        payload: SafeWorkRequest,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        # PR32 production Projects are the only INBOX objects PR35 bootstraps in bulk.
        # Raw source Projects stay source material and never become review work merely
        # because the user asked the desktop to continue safe computation.
        prepared = 0
        prepare_failed = 0
        for project in presets.list_projects(limit=500):
            if bool(project.metadata.get("pr10_review_initialized")):
                continue
            try:
                review.bootstrap_project(project.project_id)
                prepared += 1
            except (ReviewError, TypeError, ValueError):
                prepare_failed += 1

        queue = review.list_queue(limit=500, include_auto=False)
        queue_items = queue.get("items")
        ready_raw = queue.get("ready_projects")
        candidates: list[tuple[str, str]] = []
        if isinstance(ready_raw, list):
            candidates.extend(
                ("render_final", project_id)
                for project_id in ready_raw
                if isinstance(project_id, str)
            )
        if isinstance(queue_items, list):
            for item in queue_items:
                if not isinstance(item, Mapping) or not _preview_safe(item):
                    continue
                project_id = item.get("project_id")
                if isinstance(project_id, str):
                    candidates.append(("render_preview", project_id))

        unique: dict[tuple[str, str], None] = {}
        for candidate in candidates:
            unique.setdefault(candidate, None)
        ordered = sorted(
            unique,
            key=lambda item: (_RENDER_OPERATION_PRIORITY[item[0]], item[1]),
        )

        results: list[dict[str, object]] = []
        for operation, project_id in ordered[: payload.render_limit]:
            try:
                if operation == "render_final":
                    review.render_final(project_id)
                else:
                    review.render_preview(project_id)
                results.append(
                    {
                        "project_id": project_id,
                        "operation": operation,
                        "outcome": "succeeded",
                    }
                )
            except (ReviewError, TypeError, ValueError) as exc:
                results.append(
                    {
                        "project_id": project_id,
                        "operation": operation,
                        "outcome": "failed",
                        "error_code": type(exc).__name__,
                    }
                )

        return {
            "prepared": prepared,
            "prepare_failed": prepare_failed,
            "render_limit": payload.render_limit,
            "render_candidates": len(ordered),
            "rendered": len(results),
            "remaining_render_candidates": max(0, len(ordered) - len(results)),
            "results": results,
            "attention": snapshot(100),
        }


__all__ = ["SafeWorkRequest", "install_daily_production_routes"]
