from __future__ import annotations

import hashlib

from trading_control_plane.analytics import ANALYTICS_DATASET_VERSION, AnalyticsDataset
from trading_control_plane.report_engines import ReportArtifact
from trading_control_plane.service_component import ServiceComponent

# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class AnalyticsReportService(ServiceComponent):
    def persist_report(
        self,
        actor_id: UUID,
        dataset: AnalyticsDataset,
        artifact: ReportArtifact,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        payload = {
            "engine": artifact.engine,
            "environment": dataset.scope.environment,
            "generation": dataset.scope.generation,
            "account_ids": list(dataset.scope.account_ids),
            "venues": list(dataset.scope.venues),
            "from_time": dataset.scope.from_time.isoformat(),
            "to_time": dataset.scope.to_time.isoformat(),
        }
        with self.database.session_factory.begin() as session:
            user, workspace, team = self.transactions._active_scope(session, actor_id)
            assert team is not None
            if (
                workspace.workspace_id != dataset.scope.workspace_id
                or team.team_id != dataset.scope.team_id
            ):
                raise DomainRejected(
                    "ANALYTICS_SCOPE_CHANGED",
                    "active Workspace or Team changed while generating the report",
                )
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=str(actor_id),
                operation="GENERATE_ANALYTICS_REPORT",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            report = AnalyticsReport(
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                created_by=user.user_id,
                engine=artifact.engine,
                library_name=artifact.library,
                library_version=artifact.library_version,
                dataset_version=ANALYTICS_DATASET_VERSION,
                environment=dataset.scope.environment,
                generation=dataset.scope.generation,
                account_ids=list(dataset.scope.account_ids),
                venues=list(dataset.scope.venues),
                from_time=dataset.scope.from_time,
                to_time=dataset.scope.to_time,
                status="READY",
                metrics=artifact.metrics,
                chart_count=artifact.chart_count,
                coverage=dataset.coverage,
                report_metadata={
                    **dataset.metadata,
                    "readiness": artifact.readiness,
                    "engine": artifact.engine,
                    "library": artifact.library,
                    "library_version": artifact.library_version,
                    "generated_from_same_standardized_dataset": True,
                    "external_market_downloads": False,
                    "exchange_write_adapter_calls": 0,
                },
                artifact_html=artifact.html,
                artifact_sha256=hashlib.sha256(artifact.html.encode("utf-8")).hexdigest(),
                idempotency_key=idempotency_key,
                correlation_id=uuid4(),
                version=1,
                generated_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(report)
            session.flush()
            response = {
                "report_id": str(report.report_id),
                "status": report.status,
                "engine": report.engine,
            }
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="ANALYTICS_REPORT_GENERATED",
                object_type="AnalyticsReport",
                object_id=report.report_id,
                reason="read-only report generated from trusted normalized analytics facts",
                correlation_id=report.correlation_id,
                object_version=report.version,
                idempotency_key=idempotency_key,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                environment=report.environment,
                generation=report.generation,
                rule_summary={
                    "engine": report.engine,
                    "library_version": report.library_version,
                    "from_time": report.from_time.isoformat(),
                    "to_time": report.to_time.isoformat(),
                    "coverage": report.coverage,
                    "artifact_sha256": report.artifact_sha256,
                    "exchange_write_adapter_calls": 0,
                },
                now=now,
            )
            self.transactions._save_receipt(
                session,
                caller_id=str(actor_id),
                operation="GENERATE_ANALYTICS_REPORT",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            return response
