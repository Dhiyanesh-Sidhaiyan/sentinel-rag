# Deployment Standards

## Pipeline

All services deploy through Argo CD using GitOps. Argo CD replaces the legacy Jenkins Deploy pipeline, which was retired in 2025. Every change must pass unit tests, a container vulnerability scan with Trivy, and a staging soak of at least 30 minutes before reaching production.

## Progressive Delivery

Production deployments use canary releases managed by Argo Rollouts. Traffic shifts 5%, 25%, 50%, then 100%, with automated analysis against Datadog metrics at each step. A canary is rolled back automatically if the error rate exceeds 1% or p99 latency regresses by more than 20%.

## Change Freeze

A change freeze applies from December 15 to January 5 every year. During the freeze only SEV-1 fixes may be deployed, and every deployment requires approval from the Incident Commander on duty.

## Rollback

Rollbacks must be possible within 5 minutes. Database migrations must be backward compatible for at least one release so that the previous application version keeps working after a rollback.
