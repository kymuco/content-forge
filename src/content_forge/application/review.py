"""Public PR10 review service with adversarial-review hardening.

The first implementation remains in ``_review_base``; this facade owns the hardened
public authority checks and lifecycle invariants without changing PR7 render authority
or granting the phone a generic Project mutation surface.
"""

from __future__ import annotations

from pydantic import JsonValue

from content_forge.core import (
    AttentionMode,
    Project,
    ProjectState,
    ReviewPriority,
    ReviewStatus,
)

from . import _review_base as _base

ReviewConflictError = _base.ReviewConflictError
ReviewError = _base.ReviewError
ReviewNotFoundError = _base.ReviewNotFoundError
ReviewNotReadyError = _base.ReviewNotReadyError
ReviewRenderError = _base.ReviewRenderError
ReviewValidationError = _base.ReviewValidationError


_TASK_AUTHORITY = {
    _base._AUTO_BOOTSTRAP_TASK: (AttentionMode.AUTO, ReviewPriority.LOW, False),
    "hook": (AttentionMode.REVIEW, ReviewPriority.BLOCKING, True),
    "crop_confirmation": (AttentionMode.REVIEW, ReviewPriority.HIGH, True),
    "source_order": (AttentionMode.REVIEW, ReviewPriority.HIGH, True),
    "metadata": (AttentionMode.REVIEW, ReviewPriority.NORMAL, False),
    _base._PREVIEW_TASK: (AttentionMode.REVIEW, ReviewPriority.BLOCKING, True),
    "source_setup": (AttentionMode.MANUAL, ReviewPriority.BLOCKING, True),
}
_BOOTSTRAP_FORBIDDEN_UNINITIALIZED = frozenset(
    {ProjectState.READY, ProjectState.RENDERING, ProjectState.QC, ProjectState.DONE}
)
_MANUAL_REENTRY_PENDING = "pr10_manual_reentry_pending"
_MANUAL_REENTRY_RECEIPT = "pr10_manual_reentry_receipt_job_id"
_MANUAL_REENTRY_JOB_TYPE = "review_manual_reentry"
_MANUAL_REENTRY_JOB_STATE = "running"
_MANUAL_REENTRY_DONE_STATE = "succeeded"


class ReviewService(_base.ReviewService):
    """PR10 review service with closed task authority and recoverable manual re-entry."""

    _MANUAL_RECHECK_STATES = frozenset({ProjectState.INBOX, ProjectState.NEEDS_REVIEW})

    @staticmethod
    def _validate_reserved_task_authority(project: Project) -> None:
        """Fail closed on duplicate or authority-mismatched PR10-reserved task types."""

        seen: set[str] = set()
        for task in project.review_tasks:
            expected = _TASK_AUTHORITY.get(task.task_type)
            if expected is None:
                continue
            if task.task_type in seen:
                raise ReviewConflictError(
                    f"duplicate reserved review task type: {task.task_type}"
                )
            seen.add(task.task_type)
            attention, priority, blocking = expected
            if (
                task.project_id != project.project_id
                or task.attention is not attention
                or task.priority is not priority
                or task.blocking is not blocking
            ):
                raise ReviewConflictError(
                    f"reserved review task authority collision: {task.task_type}"
                )

    @staticmethod
    def _reject_uninitialized_reserved_tasks(project: Project) -> None:
        """Never adopt pre-existing state from PR10's reserved task namespace."""

        reserved = sorted(
            task.task_type
            for task in project.review_tasks
            if task.task_type in _TASK_AUTHORITY
        )
        if reserved:
            raise ReviewConflictError(
                "uninitialized project already contains reserved review task: "
                + ", ".join(reserved)
            )

    @staticmethod
    def _job_payload_json(job: _base.StoredJob) -> str:
        payload = job.model_dump(mode="json")["payload"]
        return _base.json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def _manual_reentry_receipt(
        self,
        project: Project,
    ) -> tuple[_base.StoredJob, str]:
        """Load and validate the independent SQLite proof for a MANUAL re-entry."""

        if project.metadata.get(_MANUAL_REENTRY_PENDING) is not True:
            raise ReviewConflictError("manual re-entry checkpoint is not active")
        receipt_id = project.metadata.get(_MANUAL_REENTRY_RECEIPT)
        if not isinstance(receipt_id, str):
            raise ReviewConflictError("manual re-entry checkpoint has no durable receipt")
        if project.state not in self._MANUAL_RECHECK_STATES:
            raise ReviewConflictError(
                "manual re-entry checkpoint is outside an editable review state"
            )

        self._validate_reserved_task_authority(project)
        source_setup = self._task(project, "source_setup")
        bootstrap = self._task(project, _base._AUTO_BOOTSTRAP_TASK)
        if (
            source_setup is None
            or source_setup.status is not ReviewStatus.OPEN
            or bootstrap is None
            or bootstrap.status is not ReviewStatus.RESOLVED
            or bootstrap.accepted_value != "manual_setup_required"
        ):
            raise ReviewConflictError("manual re-entry task lifecycle is not resumable")

        with self.library.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            raise ReviewConflictError("manual re-entry durable receipt is missing")

        try:
            raw_payload = str(row["payload_json"])
            receipt = _base.StoredJob(
                job_id=row["job_id"],
                project_id=row["project_id"],
                job_type=row["job_type"],
                state=row["state"],
                payload=_base.json.loads(raw_payload),
                created_at=_base.datetime.fromisoformat(row["created_at"]),
                updated_at=_base.datetime.fromisoformat(row["updated_at"]),
            )
        except Exception as exc:
            raise ReviewConflictError("manual re-entry durable receipt is malformed") from exc

        if (
            receipt.project_id != project.project_id
            or receipt.job_type != _MANUAL_REENTRY_JOB_TYPE
            or receipt.state != _MANUAL_REENTRY_JOB_STATE
            or receipt.payload.get("purpose") != "pr10_manual_reentry"
            or receipt.payload.get("source_setup_task_id")
            != source_setup.review_task_id
            or receipt.payload.get("bootstrap_task_id") != bootstrap.review_task_id
            or receipt.payload.get("receipt_version") != 1
        ):
            raise ReviewConflictError("manual re-entry durable receipt does not match project")
        return receipt, raw_payload

    def _validate_mutation_authority(self, project: Project) -> None:
        """Validate authority at the exact CAS mutation snapshot."""

        pending = project.metadata.get(_MANUAL_REENTRY_PENDING) is True
        initialized = bool(project.metadata.get("pr10_review_initialized"))
        if pending:
            self._manual_reentry_receipt(project)
            return
        if initialized:
            self._validate_reserved_task_authority(project)
            return
        self._reject_uninitialized_reserved_tasks(project)

    def _mutate_project(
        self,
        project_id: str,
        mutation,
        *,
        retries: int = 3,
    ) -> Project:
        """Guard every facade mutation at the exact manifest snapshot used by CAS."""

        def guarded(project: Project) -> Project:
            self._validate_mutation_authority(project)
            return mutation(project)

        return super()._mutate_project(project_id, guarded, retries=retries)

    def _require_initialized_authority(self, project: Project) -> Project:
        """Require the PR10 initialization receipt before any reserved-task operation."""

        if project.metadata.get(_MANUAL_REENTRY_PENDING) is True:
            raise ReviewConflictError("manual re-entry must finish before phone review")
        if not bool(project.metadata.get("pr10_review_initialized")):
            raise ReviewConflictError("project is not initialized for PR10 review")
        self._validate_reserved_task_authority(project)
        return project

    def _validate_manual_reentry_source(self, project: Project) -> None:
        """Require the exact canonical non-renderable lifecycle before issuing a receipt."""

        self._require_initialized_authority(project)
        if (
            bool(project.metadata.get("review_renderable"))
            or project.state not in self._MANUAL_RECHECK_STATES
        ):
            raise ReviewConflictError("project is not eligible for manual re-entry")
        reserved = {
            task.task_type
            for task in project.review_tasks
            if task.task_type in _TASK_AUTHORITY
        }
        if reserved != {_base._AUTO_BOOTSTRAP_TASK, "source_setup"}:
            raise ReviewConflictError("manual re-entry source has unexpected reserved tasks")
        source_setup = self._task(project, "source_setup")
        bootstrap = self._task(project, _base._AUTO_BOOTSTRAP_TASK)
        if (
            source_setup is None
            or source_setup.status is not ReviewStatus.OPEN
            or bootstrap is None
            or bootstrap.status is not ReviewStatus.RESOLVED
            or bootstrap.accepted_value != "manual_setup_required"
        ):
            raise ReviewConflictError("manual re-entry source lifecycle is not canonical")

    def _begin_manual_reentry(self, project_id: str, *, retries: int = 3) -> Project:
        """Atomically create an independent receipt and clear PR10 initialization."""

        last: ReviewConflictError | None = None
        for _ in range(retries):
            current, expected_json = self._project_snapshot(project_id)
            self._validate_manual_reentry_source(current)
            source_setup = self._task(current, "source_setup")
            bootstrap = self._task(current, _base._AUTO_BOOTSTRAP_TASK)
            assert source_setup is not None and bootstrap is not None

            receipt = _base.StoredJob(
                project_id=project_id,
                job_type=_MANUAL_REENTRY_JOB_TYPE,
                state=_MANUAL_REENTRY_JOB_STATE,
                payload={
                    "purpose": "pr10_manual_reentry",
                    "receipt_version": 1,
                    "source_setup_task_id": source_setup.review_task_id,
                    "bootstrap_task_id": bootstrap.review_task_id,
                },
            )
            metadata = dict(current.metadata)
            metadata[_MANUAL_REENTRY_PENDING] = True
            metadata[_MANUAL_REENTRY_RECEIPT] = receipt.job_id
            metadata.pop("pr10_review_initialized", None)
            updated = current.validated_copy(
                update={"metadata": metadata, "updated_at": _base._utc_now()}
            )
            serialized = _base.dump_json(updated)
            try:
                with self.library.database.transaction() as connection:
                    changed = connection.execute(
                        """
                        UPDATE projects
                        SET content_kind = ?, state = ?, manifest_json = ?, updated_at = ?
                        WHERE project_id = ? AND manifest_json = ?
                        """,
                        (
                            updated.content_kind,
                            updated.state.value,
                            serialized,
                            updated.updated_at.isoformat(),
                            updated.project_id,
                            expected_json,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise ReviewConflictError(
                            f"project changed concurrently: {project_id}"
                        )
                    connection.execute(
                        """
                        INSERT INTO jobs(
                            job_id, project_id, job_type, state,
                            payload_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            receipt.job_id,
                            receipt.project_id,
                            receipt.job_type,
                            receipt.state,
                            self._job_payload_json(receipt),
                            receipt.created_at.isoformat(),
                            receipt.updated_at.isoformat(),
                        ),
                    )
                return updated
            except ReviewConflictError as exc:
                last = exc
        raise last or ReviewConflictError(f"project changed concurrently: {project_id}")

    def _complete_manual_reentry(
        self,
        project_id: str,
        mutation,
        *,
        retries: int = 3,
    ) -> Project:
        """Atomically finalize the Project and consume its active SQLite receipt."""

        last: ReviewConflictError | None = None
        for _ in range(retries):
            current, expected_json = self._project_snapshot(project_id)
            receipt, expected_payload_json = self._manual_reentry_receipt(current)
            updated = mutation(current)
            if updated == current:
                raise ReviewConflictError("manual re-entry finalization made no progress")
            serialized = _base.dump_json(updated)
            receipt_updated_at = _base._utc_now()
            try:
                with self.library.database.transaction() as connection:
                    changed = connection.execute(
                        """
                        UPDATE projects
                        SET content_kind = ?, state = ?, manifest_json = ?, updated_at = ?
                        WHERE project_id = ? AND manifest_json = ?
                        """,
                        (
                            updated.content_kind,
                            updated.state.value,
                            serialized,
                            updated.updated_at.isoformat(),
                            updated.project_id,
                            expected_json,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise ReviewConflictError(
                            f"project changed concurrently: {project_id}"
                        )
                    receipt_changed = connection.execute(
                        """
                        UPDATE jobs
                        SET state = ?, updated_at = ?
                        WHERE job_id = ? AND project_id = ? AND job_type = ?
                          AND state = ? AND payload_json = ?
                        """,
                        (
                            _MANUAL_REENTRY_DONE_STATE,
                            receipt_updated_at.isoformat(),
                            receipt.job_id,
                            project_id,
                            _MANUAL_REENTRY_JOB_TYPE,
                            _MANUAL_REENTRY_JOB_STATE,
                            expected_payload_json,
                        ),
                    ).rowcount
                    if receipt_changed != 1:
                        raise ReviewConflictError(
                            "manual re-entry durable receipt changed concurrently"
                        )
                return updated
            except ReviewConflictError as exc:
                last = exc
        raise last or ReviewConflictError(f"project changed concurrently: {project_id}")

    def _canonical_edit_payload(
        self,
        project: Project,
        task_type: str,
    ) -> dict[str, JsonValue]:
        ordered_scenes = sorted(project.scenes, key=lambda item: item.order)
        if task_type == "hook":
            variant = self._review_variant(project)
            return {"variant_id": variant.variant_id, "current": variant.hook}
        if task_type == "crop_confirmation":
            return {
                "scene_ids": [scene.scene_id for scene in ordered_scenes],
                "crops": {
                    scene.scene_id: (
                        None if scene.crop is None else scene.crop.model_dump(mode="json")
                    )
                    for scene in ordered_scenes
                },
            }
        if task_type == "source_order":
            return {"scene_ids": [scene.scene_id for scene in ordered_scenes]}
        if task_type == "metadata":
            variant = self._review_variant(project)
            return {
                "variant_id": variant.variant_id,
                "title": variant.title,
                "description": variant.description,
                "hashtags": list(variant.hashtags),
            }
        raise ReviewValidationError(f"unsupported editable review task: {task_type}")

    def _finalize_bootstrap_payloads(
        self,
        project_id: str,
        *,
        manual_reentry: bool,
    ) -> Project:
        """Normalize phone payloads and finish a durable MANUAL re-entry checkpoint."""

        def finalize(project: Project) -> Project:
            self._validate_reserved_task_authority(project)
            if project.state is not ProjectState.NEEDS_REVIEW:
                if manual_reentry:
                    raise ReviewConflictError(
                        "manual re-entry did not return to needs_review"
                    )
                return project

            now = _base._utc_now()
            metadata = dict(project.metadata)
            changed = False
            if manual_reentry:
                if metadata.pop(_MANUAL_REENTRY_PENDING, None) is not None:
                    changed = True
                if metadata.pop(_MANUAL_REENTRY_RECEIPT, None) is not None:
                    changed = True

            renderable = bool(project.metadata.get("review_renderable"))
            tasks = []
            expected_auto_payload = {
                "template_id": _base.HOOK_OVERLAY_TEMPLATE_ID,
                "preview_profile_id": _base.SHORTS_PREVIEW_PROFILE_ID,
            }
            for task in project.review_tasks:
                replacement = task
                if (
                    manual_reentry
                    and renderable
                    and task.task_type == _base._AUTO_BOOTSTRAP_TASK
                ):
                    if (
                        task.status is not ReviewStatus.RESOLVED
                        or task.accepted_value != "prepared"
                        or dict(task.payload) != expected_auto_payload
                    ):
                        replacement = task.validated_copy(
                            update={
                                "status": ReviewStatus.RESOLVED,
                                "accepted_value": "prepared",
                                "resolved_at": now,
                                "payload": expected_auto_payload,
                            }
                        )
                elif (
                    manual_reentry
                    and renderable
                    and task.task_type == "source_setup"
                    and task.status is ReviewStatus.OPEN
                ):
                    payload = {
                        str(key): _base._plain_json(value)
                        for key, value in task.payload.items()
                    }
                    payload["status"] = "completed"
                    replacement = task.validated_copy(
                        update={
                            "status": ReviewStatus.RESOLVED,
                            "accepted_value": "manual_setup_completed",
                            "resolved_at": now,
                            "payload": payload,
                        }
                    )
                elif (
                    renderable
                    and task.attention is AttentionMode.REVIEW
                    and task.task_type in _base._EDIT_TASKS
                    and task.status is ReviewStatus.OPEN
                ):
                    payload = self._canonical_edit_payload(project, task.task_type)
                    if dict(task.payload) != payload:
                        replacement = task.validated_copy(update={"payload": payload})

                if replacement != task:
                    changed = True
                tasks.append(replacement)

            if not changed:
                return project
            return project.validated_copy(
                update={
                    "review_tasks": tuple(tasks),
                    "metadata": metadata,
                    "updated_at": now,
                }
            )

        if manual_reentry:
            return self._complete_manual_reentry(project_id, finalize)
        return self._mutate_project(project_id, finalize)

    def bootstrap_project(self, project_id: str) -> Project:
        """Bootstrap editable states and resume MANUAL repair across crash checkpoints."""

        current = self.get_project(project_id)
        initialized = bool(current.metadata.get("pr10_review_initialized"))
        pending = current.metadata.get(_MANUAL_REENTRY_PENDING) is True

        if pending:
            self._manual_reentry_receipt(current)
            if initialized:
                return self._finalize_bootstrap_payloads(
                    project_id,
                    manual_reentry=True,
                )
            prepared = super().bootstrap_project(project_id)
            self._manual_reentry_receipt(prepared)
            return self._finalize_bootstrap_payloads(
                project_id,
                manual_reentry=True,
            )

        # Before PR10 has established its initialization receipt it owns none of the
        # reserved task namespace. Even a structurally matching task may carry arbitrary
        # status/payload/accepted-value lifecycle state, so adopting it by type is unsafe.
        if not initialized:
            self._reject_uninitialized_reserved_tasks(current)
        else:
            self._validate_reserved_task_authority(current)

        # READY is intentionally editable after PR10 approval, but it is never a legal
        # first-bootstrap input. Older workflow projects must not be rewound into PR10.
        if not initialized and current.state in _BOOTSTRAP_FORBIDDEN_UNINITIALIZED:
            raise ReviewConflictError(
                f"project cannot be bootstrapped from state {current.state.value}"
            )

        should_recheck = (
            initialized
            and not bool(current.metadata.get("review_renderable"))
            and current.state in self._MANUAL_RECHECK_STATES
        )
        if initialized and not should_recheck:
            return current

        if should_recheck:
            begun = self._begin_manual_reentry(project_id)
            if begun.metadata.get(_MANUAL_REENTRY_PENDING) is not True:
                return begun
            # Re-enter through the durable receipt path. If the process exits before
            # or after base bootstrap, the next call observes the same independently
            # authenticated checkpoint and resumes safely.
            return self.bootstrap_project(project_id)

        prepared = super().bootstrap_project(project_id)
        self._validate_reserved_task_authority(prepared)
        return self._finalize_bootstrap_payloads(
            project_id,
            manual_reentry=False,
        )

    def _list_projects(self) -> tuple[Project, ...]:
        """Enumerate projects per row so one malformed manifest is quarantined."""

        with self.library.database.connection() as connection:
            rows = connection.execute(
                "SELECT project_id, manifest_json FROM projects "
                "ORDER BY updated_at DESC, project_id"
            ).fetchall()
        projects: list[Project] = []
        for row in rows:
            try:
                project = _base.load_json(Project, str(row["manifest_json"]))
            except Exception:
                continue
            if project.project_id != str(row["project_id"]):
                continue
            projects.append(project)
        return tuple(projects)

    def _matching_jobs(
        self,
        project_id: str,
        *,
        purpose: str,
        plan_digest: str,
        states: tuple[str, ...],
    ) -> tuple[_base.StoredJob, ...]:
        """Scan persisted jobs per row and quarantine malformed render records."""

        if not states:
            return ()
        placeholders = ",".join("?" for _ in states)
        with self.library.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE project_id = ? AND job_type = 'render'
                  AND state IN ({placeholders})
                ORDER BY updated_at DESC, job_id DESC
                """,
                (project_id, *states),
            ).fetchall()
        matches: list[_base.StoredJob] = []
        for row in rows:
            try:
                job = _base.StoredJob(
                    job_id=row["job_id"],
                    project_id=row["project_id"],
                    job_type=row["job_type"],
                    state=row["state"],
                    payload=_base.json.loads(row["payload_json"]),
                    created_at=_base.datetime.fromisoformat(row["created_at"]),
                    updated_at=_base.datetime.fromisoformat(row["updated_at"]),
                )
            except Exception:
                continue
            if (
                job.payload.get("purpose") == purpose
                and job.payload.get("render_plan_digest") == plan_digest
            ):
                matches.append(job)
        return tuple(matches)

    def list_queue(
        self,
        *,
        limit: int = 100,
        include_auto: bool = False,
    ) -> dict[str, object]:
        """Authority-filter the full ordered queue before applying the caller limit."""

        if limit < 1 or limit > 500:
            raise ReviewValidationError("limit must be between 1 and 500")

        projects = self._list_projects()
        valid_projects: list[Project] = []
        for project in projects:
            if (
                not bool(project.metadata.get("pr10_review_initialized"))
                or project.metadata.get(_MANUAL_REENTRY_PENDING) is True
            ):
                continue
            try:
                self._validate_reserved_task_authority(project)
            except ReviewConflictError:
                continue
            valid_projects.append(project)

        queue: list[dict[str, object]] = []
        for project in valid_projects:
            if project.state in _base._TERMINAL_REVIEW_STATES:
                continue
            for task in project.review_tasks:
                if task.status is not ReviewStatus.OPEN:
                    continue
                if not include_auto and task.attention is AttentionMode.AUTO:
                    continue
                queue.append(
                    {
                        "project_id": project.project_id,
                        "project_state": project.state.value,
                        "content_kind": str(project.content_kind),
                        "task": task.model_dump(mode="json"),
                    }
                )
        queue.sort(
            key=lambda item: (
                0 if item["task"]["blocking"] else 1,
                _base._PRIORITY_RANK[ReviewPriority(item["task"]["priority"])],
                _base._ATTENTION_RANK[AttentionMode(item["task"]["attention"])],
                item["task"]["created_at"],
                item["project_id"],
                item["task"]["review_task_id"],
            )
        )

        ready_projects = [
            self.project_summary(project)
            for project in sorted(valid_projects, key=lambda item: item.project_id)
            if self._is_final_render_candidate(project)
        ]
        return {"items": queue[:limit], "ready_projects": ready_projects}

    def _is_final_render_candidate(self, project: Project) -> bool:
        if (
            project.state is not ProjectState.READY
            or not bool(project.metadata.get("pr10_review_initialized"))
            or project.metadata.get(_MANUAL_REENTRY_PENDING) is True
            or not bool(project.metadata.get("review_renderable"))
        ):
            return False
        try:
            self._validate_reserved_task_authority(project)
        except ReviewConflictError:
            return False
        job_id = project.metadata.get("approved_preview_job_id")
        digest = project.metadata.get("approved_preview_plan_digest")
        revision = project.metadata.get("approved_preview_revision_digest")
        if not all(isinstance(value, str) for value in (job_id, digest, revision)):
            return False
        preview = self._task(project, _base._PREVIEW_TASK)
        if (
            preview is None
            or preview.status is not ReviewStatus.RESOLVED
            or preview.accepted_value != job_id
        ):
            return False
        return _base._preview_revision_digest(project) == revision

    def resolve_task(
        self,
        project_id: str,
        task_id: str,
        value: JsonValue | None,
    ) -> Project:
        self._require_initialized_authority(self.get_project(project_id))
        return super().resolve_task(project_id, task_id, value)

    def render_preview(self, project_id: str) -> dict[str, object]:
        self._require_initialized_authority(self.get_project(project_id))
        return super().render_preview(project_id)

    def approve_preview(self, project_id: str, job_id: str) -> Project:
        self._require_initialized_authority(self.get_project(project_id))
        return super().approve_preview(project_id, job_id)

    def reject_preview(
        self,
        project_id: str,
        job_id: str,
        *,
        feedback: str | None = None,
    ) -> Project:
        """Reject a preview while reopening edit cards from canonical current values."""

        if feedback is not None and len(feedback) > 4096:
            raise ReviewValidationError("preview feedback is too long")
        self._require_initialized_authority(self.get_project(project_id))

        def rehydrate_resolved_edits(project: Project) -> Project:
            self._require_initialized_authority(project)
            preview = self._task(project, _base._PREVIEW_TASK)
            if (
                preview is None
                or preview.status is not ReviewStatus.OPEN
                or preview.payload.get("job_id") != job_id
            ):
                raise ReviewConflictError("preview job is not the current approval candidate")

            changed = False
            tasks = []
            for task in project.review_tasks:
                replacement = task
                if (
                    task.attention is AttentionMode.REVIEW
                    and task.task_type in _base._EDIT_TASKS
                    and task.status is ReviewStatus.RESOLVED
                ):
                    payload = self._canonical_edit_payload(project, task.task_type)
                    if dict(task.payload) != payload:
                        replacement = task.validated_copy(update={"payload": payload})
                if replacement != task:
                    changed = True
                tasks.append(replacement)

            if not changed:
                return project
            return project.validated_copy(
                update={"review_tasks": tuple(tasks), "updated_at": _base._utc_now()}
            )

        # Resolved tasks are not visible/editable in the phone queue, so refreshing their
        # payload before the rejection commit cannot expose an intermediate stale form.
        # The project manifest CAS serializes this with competing phone mutations.
        self._mutate_project(project_id, rehydrate_resolved_edits)
        return super().reject_preview(project_id, job_id, feedback=feedback)

    def render_final(self, project_id: str) -> dict[str, object]:
        self._require_initialized_authority(self.get_project(project_id))
        return super().render_final(project_id)

    def reconcile_persisted_state(self) -> None:
        """Recover valid PR10 projects while quarantining invalid project/job rows."""

        projects = self._list_projects()
        valid_projects: list[Project] = []
        valid_ids: set[str] = set()
        for project in projects:
            if (
                not bool(project.metadata.get("pr10_review_initialized"))
                or project.metadata.get(_MANUAL_REENTRY_PENDING) is True
            ):
                continue
            try:
                self._validate_reserved_task_authority(project)
            except ReviewConflictError:
                # Quarantine this manifest in place. Public operations remain fail-closed,
                # but one invalid Project must not prevent independent valid recovery.
                continue
            valid_projects.append(project)
            valid_ids.add(project.project_id)

        # Reset only validated preview claims. A queued immutable job can be adopted by a
        # later request; invalid projects and their claims remain untouched for diagnosis.
        for project in valid_projects:
            preview = self._task(project, _base._PREVIEW_TASK)
            if (
                preview is not None
                and preview.status is ReviewStatus.OPEN
                and preview.payload.get("status") == "rendering"
            ):

                def reset_preview(current: Project) -> Project:
                    self._require_initialized_authority(current)
                    task = self._task(current, _base._PREVIEW_TASK)
                    if (
                        task is None
                        or task.status is not ReviewStatus.OPEN
                        or task.payload.get("status") != "rendering"
                    ):
                        return current
                    reset = task.validated_copy(
                        update={"payload": {"status": "not_rendered"}}
                    )
                    return current.validated_copy(
                        update={
                            "review_tasks": self._replace_task(current, reset),
                            "updated_at": _base._utc_now(),
                        }
                    )

                try:
                    self._mutate_project(project.project_id, reset_preview)
                except ReviewConflictError:
                    # A concurrently altered manifest is quarantined rather than turning
                    # one recovery race into an API-wide startup failure.
                    valid_ids.discard(project.project_id)

        # Retire orphaned running preview jobs only for the still-validated set. Final jobs
        # are handled by the project-specific recovery path below. Malformed jobs are
        # quarantined per row and never become an API-wide startup dependency.
        with self.library.database.connection() as connection:
            rows = connection.execute(
                "SELECT job_id, project_id, payload_json FROM jobs WHERE state = 'running'"
            ).fetchall()
        for row in rows:
            project_id = str(row["project_id"])
            if project_id not in valid_ids:
                continue
            try:
                payload = _base.json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("purpose") != "preview":
                continue
            try:
                _base.transition_job_state(
                    self.library.database,
                    str(row["job_id"]),
                    expected_state="running",
                    state="failed",
                )
            except (_base.StorageConflictError, TypeError, ValueError):
                continue

        for project in self._list_projects():
            if (
                project.project_id in valid_ids
                and project.state in {ProjectState.RENDERING, ProjectState.QC}
            ):
                try:
                    self._require_initialized_authority(project)
                    self._recover_project_after_restart(project)
                except (ReviewError, _base.StorageConflictError, TypeError, ValueError):
                    # Quarantine only the independently broken project/job path while
                    # allowing every other valid Project to finish restart recovery.
                    continue


__all__ = [
    "ReviewConflictError",
    "ReviewError",
    "ReviewNotFoundError",
    "ReviewNotReadyError",
    "ReviewRenderError",
    "ReviewService",
    "ReviewValidationError",
]
