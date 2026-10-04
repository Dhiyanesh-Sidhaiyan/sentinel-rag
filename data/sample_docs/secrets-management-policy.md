# Secrets Management Policy

## Scope

This policy applies to all credentials, API keys, certificates, and tokens used by Orbitly services and engineers.

## Storage

All production secrets must be stored in HashiCorp Vault. Secrets must never be committed to Git, written to logs, or shared in chat tools. Kubernetes workloads read secrets through the External Secrets Operator, which integrates with HashiCorp Vault.

## Rotation

Database credentials are rotated automatically every 30 days. Third-party API keys must be rotated at least every 90 days. Any credential that is suspected to be exposed must be revoked and rotated immediately, and the exposure must be reported to the Security Team within 1 hour.

## Ownership

HashiCorp Vault is managed by Team Sentinel. Access to production secret paths requires approval from a service owner and is granted for a maximum of 12 hours through just-in-time access. All Vault access is audited and the audit log stores data in Splunk.

## Enforcement

Pre-commit hooks and CI secret scanning block commits that contain credentials. Violations of this policy are reviewed by the Security Team.
