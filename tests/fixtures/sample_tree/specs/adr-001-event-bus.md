# ADR-001: Adopt an event bus

Date: 2024-02-01

## Decision

Services communicate through the event bus instead of direct calls.

## Consequences

The order service publishes OrderCreated events.
