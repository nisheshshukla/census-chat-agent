-- One-time setup. Run in Snowsight as ACCOUNTADMIN.
-- Creates a least-privilege service user for the app, a small warehouse with a hard
-- statement timeout and a credit cap, and an app-owned database for the semantic layer.
--
-- Prerequisite: the Marketplace listing "US Open Census Data & Neighborhood Insights" has
-- been added to the account (Data Products > Marketplace > Get). The script finds its
-- database name itself. The RSA public key below is the one produced by
-- scripts/gen_keypair.sh for this deployment (public keys are not secret).
--
-- Run the whole script (Snowsight: select all, then Run All).

USE ROLE ACCOUNTADMIN;

SHOW DATABASES LIKE 'US_OPEN_CENSUS%';
SET census_share_db = (SELECT "name" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) LIMIT 1);
SELECT $census_share_db AS census_share_database;

-- Role -----------------------------------------------------------------------
CREATE ROLE IF NOT EXISTS CENSUS_READER COMMENT = 'Read-only role for the census chat agent';

-- Warehouse with a hard per-statement timeout --------------------------------
CREATE WAREHOUSE IF NOT EXISTS CENSUS_WH
  WITH WAREHOUSE_SIZE = 'XSMALL'
  AUTO_SUSPEND = 300
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = FALSE
  STATEMENT_TIMEOUT_IN_SECONDS = 20
  STATEMENT_QUEUED_TIMEOUT_IN_SECONDS = 15
  COMMENT = 'Census chat agent (XS, 5 min auto-suspend)';

-- Credit cap so a runaway loop cannot drain the trial ------------------------
CREATE RESOURCE MONITOR IF NOT EXISTS CENSUS_MONITOR
  WITH CREDIT_QUOTA = 40
  FREQUENCY = MONTHLY
  START_TIMESTAMP = IMMEDIATELY
  TRIGGERS
    ON 75 PERCENT DO NOTIFY
    ON 100 PERCENT DO SUSPEND_IMMEDIATE;
ALTER WAREHOUSE CENSUS_WH SET RESOURCE_MONITOR = CENSUS_MONITOR;

-- App-owned database for the semantic layer (metadata copies + Cortex Search) -
CREATE DATABASE IF NOT EXISTS CENSUS_APP_DB COMMENT = 'Census chat agent semantic layer';
CREATE SCHEMA IF NOT EXISTS CENSUS_APP_DB.SEMANTIC;

-- Service user with key-pair auth (no password, no MFA prompt) ---------------
CREATE USER IF NOT EXISTS CENSUS_APP
  TYPE = SERVICE
  DEFAULT_ROLE = CENSUS_READER
  DEFAULT_WAREHOUSE = CENSUS_WH
  COMMENT = 'Census chat agent service user';
ALTER USER CENSUS_APP SET RSA_PUBLIC_KEY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAx8/aRGsCTiJsft2lKPFRJPekJ06eCCI7x81/R+mnjvcJPGjfJIAkwRPrupxd7qGKU+GdA4y90LzjgQSU+icH/AcViLMiA6DnOR6TiuVzP9vsBvESxzTI9lYj/uVm29cqENXeHdopKjBGg4PmocZRqajHS7P8mqCXutqaJk8RSGR5zklep8PGmsycplIdDPKbr1y+tjXIqwg76j5eRnJCUgNPyAdMduBXuHVMOjFXAa13DNZstYOMQoR4eh1kaCxnRMQNMPJkGg6UG203IFc5/R1TqqvhgpipGp1MlnGhEWIwx1rZ0v9HQGsOYJm4Taw1gnHKjX6Lz149x4GzlEjUHQIDAQAB';
GRANT ROLE CENSUS_READER TO USER CENSUS_APP;

-- Grants ---------------------------------------------------------------------
GRANT USAGE ON WAREHOUSE CENSUS_WH TO ROLE CENSUS_READER;
GRANT IMPORTED PRIVILEGES ON DATABASE IDENTIFIER($census_share_db) TO ROLE CENSUS_READER;
GRANT USAGE ON DATABASE CENSUS_APP_DB TO ROLE CENSUS_READER;
GRANT USAGE, CREATE TABLE, CREATE VIEW, CREATE CORTEX SEARCH SERVICE
  ON SCHEMA CENSUS_APP_DB.SEMANTIC TO ROLE CENSUS_READER;
GRANT SELECT ON ALL TABLES IN SCHEMA CENSUS_APP_DB.SEMANTIC TO ROLE CENSUS_READER;
GRANT SELECT ON FUTURE TABLES IN SCHEMA CENSUS_APP_DB.SEMANTIC TO ROLE CENSUS_READER;
GRANT OWNERSHIP ON SCHEMA CENSUS_APP_DB.SEMANTIC TO ROLE CENSUS_READER COPY CURRENT GRANTS;

-- Verify ---------------------------------------------------------------------
SHOW GRANTS TO ROLE CENSUS_READER;
DESC USER CENSUS_APP;
