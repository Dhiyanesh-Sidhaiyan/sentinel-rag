# Payments Platform Architecture

## Overview

The Payments API is the public entry point for all card and ACH transactions at Orbitly. The Payments API is owned by Team Atlas. Team Atlas reports to the VP of Platform Engineering. The service runs on Kubernetes in two regions (us-east-1 and us-west-2) in an active-active configuration.

## Dependencies

The Payments API depends on Ledger Service and Fraud Engine. Ledger Service stores data in Aurora PostgreSQL and is owned by Team Atlas. Fraud Engine is owned by Team Sentinel and integrates with Stripe Radar for real-time risk scoring. Fraud Engine publishes to Kafka topic payments.risk-events.

Ledger Service uses double-entry bookkeeping. Every transaction produces exactly two balanced entries, and entries are immutable once written. Corrections are made by posting a reversing entry, never by updating rows.

## Service Levels

The Payments API has an availability SLO of 99.95% measured monthly. The p99 latency objective is 300 ms for authorization requests. Error budget burn above 2x for one hour pages the on-call engineer automatically.

## Data Retention

Raw card numbers are never stored by Orbitly systems. Card data is tokenized by Stripe before it reaches the Payments API. Transaction records are retained for 7 years to satisfy financial audit requirements, after which they are archived to cold storage.
