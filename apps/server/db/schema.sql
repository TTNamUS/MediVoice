-- MediVoice database schema
-- Run via: psql $DATABASE_URL -f db/schema.sql

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS patients (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    full_name   TEXT NOT NULL,
    phone       TEXT UNIQUE,
    dob         DATE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS doctors (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    full_name   TEXT NOT NULL,
    specialty   TEXT NOT NULL,
    languages   TEXT[] DEFAULT '{}',
    bio         TEXT
);

CREATE TABLE IF NOT EXISTS slots (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    doctor_id   UUID REFERENCES doctors(id),
    start_at    TIMESTAMPTZ NOT NULL,
    end_at      TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL DEFAULT 'available',  -- available | booked | blocked
    UNIQUE(doctor_id, start_at)
);

CREATE TABLE IF NOT EXISTS appointments (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slot_id          UUID UNIQUE REFERENCES slots(id),
    patient_id       UUID REFERENCES patients(id),
    reason           TEXT,
    status           TEXT NOT NULL DEFAULT 'confirmed',
    idempotency_key  TEXT UNIQUE,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- PII firewall: text-to-SQL only touches this view, never raw tables
CREATE OR REPLACE VIEW appointment_summary AS
    SELECT
        a.id,
        a.reason,
        a.status,
        a.created_at,
        s.start_at,
        s.end_at,
        d.full_name  AS doctor_name,
        d.specialty,
        p.id         AS patient_id   -- no phone / dob / full_name
    FROM appointments a
    JOIN slots     s ON s.id = a.slot_id
    JOIN doctors   d ON d.id = s.doctor_id
    JOIN patients  p ON p.id = a.patient_id;
