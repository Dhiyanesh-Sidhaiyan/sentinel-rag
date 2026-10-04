# Incident Response Runbook

## Severity Levels

SEV-1 means a customer-facing outage or data loss affecting more than 5% of customers. SEV-2 means significant degradation with a workaround available. SEV-3 means a minor issue with no customer impact.

## Declaring an Incident

Any engineer can declare an incident in the #incidents Slack channel using the /incident command. Declaring early is always preferred; an incident can be downgraded later at no cost.

## Escalation

The on-call engineer escalates to the Incident Commander for every SEV-1. The Incident Commander escalates to the VP of Platform Engineering if a SEV-1 lasts longer than 30 minutes. For SEV-1 incidents the first status page update must be published within 15 minutes of declaration.

## Response Targets

SEV-1 incidents require acknowledgement within 5 minutes and a status update every 30 minutes. SEV-2 incidents require acknowledgement within 15 minutes. SEV-3 incidents are handled during business hours.

## Postmortems

A blameless postmortem is required for every SEV-1 and SEV-2 incident. The postmortem must be published within 5 business days and must include a timeline, root cause analysis, and at least one action item with an owner and due date. Postmortems are approved by the Reliability Council.
