-- One-off database bootstrap, run by a superuser / managed-instance admin.
-- Creates the schema owner (runs migrations) and the application login (member of app_role).
-- psql -v owner_pw=... -v app_pw=... -v dbname=ese_dev -f scripts/bootstrap_db.sql
--
-- The application login never owns tables. Because app_role holds only INSERT and SELECT on
-- audit_entries, an UPDATE or DELETE from the application fails at the database (NFR-12, T-15).

SELECT 'CREATE ROLE app_role NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_role') \gexec

SELECT format('CREATE ROLE ese_owner LOGIN PASSWORD %L', :'owner_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ese_owner') \gexec

SELECT format('CREATE ROLE ese_app LOGIN PASSWORD %L IN ROLE app_role', :'app_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ese_app') \gexec

SELECT format('CREATE DATABASE %I OWNER ese_owner', :'dbname')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname') \gexec

\connect :dbname
ALTER SCHEMA public OWNER TO ese_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
