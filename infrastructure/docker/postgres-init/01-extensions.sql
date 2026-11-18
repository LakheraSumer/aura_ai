CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- Separate database for the automated test suite so tests never touch dev data.
CREATE DATABASE apna_test OWNER apna;
\connect apna_test
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
