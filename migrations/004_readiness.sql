-- Readiness (spec §7): the application checks that the schema is current, so it may read the
-- list of applied migrations.
GRANT SELECT ON schema_migrations TO incidentlens_app;
