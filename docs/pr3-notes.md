# PR3 acceptance notes

PR3 is complete when the local library can ingest arbitrary local bytes once, preserve
multiple provenance records for the same bytes, persist project/job metadata in SQLite,
and safely let multiple projects reference the same immutable asset.

The implementation intentionally stops before media probing, thumbnail generation,
proxy generation, networking, rendering, or worker execution. Those remain later
roadmap milestones.
