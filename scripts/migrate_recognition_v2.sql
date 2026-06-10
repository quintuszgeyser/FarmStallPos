-- Migration: Recognition Service v2.0
-- Adds support for:
-- - Multiple embeddings per customer (multi-pose)
-- - Quality scores
-- - Decision audit trail
-- - Calibration dataset
-- - Threshold versioning

-- =====================================================================
-- 1. Add quality columns to existing biometric tables
-- =====================================================================

-- Customer faces: add quality tracking
ALTER TABLE customer_faces ADD COLUMN IF NOT EXISTS quality NUMERIC(3,2);
ALTER TABLE customer_faces ADD COLUMN IF NOT EXISTS detected_at TIMESTAMP DEFAULT NOW();
ALTER TABLE customer_faces ADD COLUMN IF NOT EXISTS camera_source VARCHAR(50);

-- Customer gaits: add quality tracking
ALTER TABLE customer_gaits ADD COLUMN IF NOT EXISTS quality NUMERIC(3,2);
ALTER TABLE customer_gaits ADD COLUMN IF NOT EXISTS detected_at TIMESTAMP DEFAULT NOW();
ALTER TABLE customer_gaits ADD COLUMN IF NOT EXISTS camera_source VARCHAR(50);

-- Customer plates: add confidence tracking
ALTER TABLE customer_plates ADD COLUMN IF NOT EXISTS confidence NUMERIC(3,2);
ALTER TABLE customer_plates ADD COLUMN IF NOT EXISTS detected_at TIMESTAMP DEFAULT NOW();

-- =====================================================================
-- 2. Update physical attributes: replace exact height with category
-- =====================================================================

ALTER TABLE customer_physical_attributes ADD COLUMN IF NOT EXISTS height_category VARCHAR(10); -- 'short', 'medium', 'tall'

-- Drop gender, skin_tone, age_range (removed from v2)
-- Keep as comments for reference:
-- ALTER TABLE customer_physical_attributes DROP COLUMN IF EXISTS gender;
-- ALTER TABLE customer_physical_attributes DROP COLUMN IF EXISTS skin_tone;
-- ALTER TABLE customer_physical_attributes DROP COLUMN IF EXISTS age_range;

-- Index on height category
CREATE INDEX IF NOT EXISTS idx_physical_attrs_height_cat ON customer_physical_attributes(height_category);

-- =====================================================================
-- 3. Decision audit log
-- =====================================================================

CREATE TABLE IF NOT EXISTS decision_audit_log (
    id SERIAL PRIMARY KEY,
    decision_id VARCHAR(36) UNIQUE NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
    decision_type VARCHAR(20) NOT NULL, -- 'linked', 'enroll', 'pending'
    customer_id INTEGER REFERENCES customers(id),
    track_id VARCHAR(50) NOT NULL,
    confidence NUMERIC(5,3),
    frame_count INTEGER,

    -- Top candidates
    top_3_candidates JSON,

    -- Scoring details
    normalized_score NUMERIC(5,3),
    biometric_ratio NUMERIC(5,3),
    available_weight NUMERIC(5,2),
    feature_breakdown JSON,
    quality_summary JSON,

    -- Threshold info
    threshold_version VARCHAR(50),
    weights_version VARCHAR(50),
    threshold_source_link VARCHAR(100), -- 'global' or 'camera:outdoor_1'
    threshold_source_pending VARCHAR(100),

    -- Context
    context JSON, -- {camera, quality, time_of_day}
    reason VARCHAR(100) -- For pending/reject
);

CREATE INDEX idx_decision_audit_timestamp ON decision_audit_log(timestamp);
CREATE INDEX idx_decision_audit_customer ON decision_audit_log(customer_id);
CREATE INDEX idx_decision_audit_type ON decision_audit_log(decision_type);
CREATE INDEX idx_decision_audit_track ON decision_audit_log(track_id);

-- =====================================================================
-- 4. Calibration dataset for threshold tuning
-- =====================================================================

-- Ground truth pairs (manual tagging by admin)
CREATE TABLE IF NOT EXISTS calibration_ground_truth (
    id SERIAL PRIMARY KEY,
    pair_id VARCHAR(36) UNIQUE NOT NULL,
    customer_id_a INTEGER NOT NULL REFERENCES customers(id),
    customer_id_b INTEGER NOT NULL REFERENCES customers(id),
    is_same_person BOOLEAN NOT NULL,
    verified_by VARCHAR(50) NOT NULL, -- admin username
    verified_at TIMESTAMP NOT NULL DEFAULT NOW(),
    notes TEXT
);

CREATE INDEX idx_calibration_gt_customer_a ON calibration_ground_truth(customer_id_a);
CREATE INDEX idx_calibration_gt_customer_b ON calibration_ground_truth(customer_id_b);
CREATE INDEX idx_calibration_gt_same ON calibration_ground_truth(is_same_person);

-- Calibration observations (snapshots with ground truth labels)
CREATE TABLE IF NOT EXISTS calibration_observations (
    id SERIAL PRIMARY KEY,
    obs_id VARCHAR(36) UNIQUE NOT NULL,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
    camera_source VARCHAR(50),

    -- Biometric signals
    face_embedding BYTEA,
    face_quality NUMERIC(3,2),
    gait_features BYTEA,
    gait_quality NUMERIC(3,2),

    -- Physical attributes
    physical_attrs JSON,

    -- Event context
    event_id VARCHAR(50),
    snapshot_path TEXT
);

CREATE INDEX idx_calibration_obs_customer ON calibration_observations(customer_id);
CREATE INDEX idx_calibration_obs_timestamp ON calibration_observations(timestamp);

-- Calibration match scores (precomputed for ROC analysis)
CREATE TABLE IF NOT EXISTS calibration_match_scores (
    id SERIAL PRIMARY KEY,
    pair_id VARCHAR(36) NOT NULL REFERENCES calibration_ground_truth(pair_id),
    obs_a_id VARCHAR(36) NOT NULL REFERENCES calibration_observations(obs_id),
    obs_b_id VARCHAR(36) NOT NULL REFERENCES calibration_observations(obs_id),

    -- Score details
    normalized_score NUMERIC(5,3),
    biometric_ratio NUMERIC(5,3),
    available_weight NUMERIC(5,2),
    feature_breakdown JSON,

    -- Computed at
    computed_at TIMESTAMP NOT NULL DEFAULT NOW(),
    weights_version VARCHAR(50)
);

CREATE INDEX idx_calibration_scores_pair ON calibration_match_scores(pair_id);

-- =====================================================================
-- 5. Threshold configuration (versioned)
-- =====================================================================

CREATE TABLE IF NOT EXISTS threshold_config (
    id SERIAL PRIMARY KEY,
    version VARCHAR(50) UNIQUE NOT NULL,

    -- Global thresholds
    global_link_threshold NUMERIC(5,3) NOT NULL,
    global_pending_threshold NUMERIC(5,3) NOT NULL,

    -- Segment-specific thresholds (JSON)
    segment_thresholds JSON,

    -- Calibration metadata
    calibration_metadata JSON,

    -- Activation
    active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    activated_at TIMESTAMP
);

CREATE INDEX idx_threshold_config_active ON threshold_config(active) WHERE active = TRUE;

-- =====================================================================
-- 6. Weights configuration (versioned)
-- =====================================================================

CREATE TABLE IF NOT EXISTS weights_config (
    id SERIAL PRIMARY KEY,
    version VARCHAR(50) UNIQUE NOT NULL,

    -- Feature weights (JSON)
    feature_weights JSON NOT NULL,

    -- Safety constraints
    max_context_contribution NUMERIC(5,2),
    biometric_requirement_enabled BOOLEAN DEFAULT TRUE,
    plate_boost_min_biometric NUMERIC(3,2) DEFAULT 0.50,
    context_min_biometric NUMERIC(3,2) DEFAULT 0.60,

    -- Activation
    active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    activated_at TIMESTAMP
);

CREATE INDEX idx_weights_config_active ON weights_config(active) WHERE active = TRUE;

-- =====================================================================
-- 7. Insert initial configurations
-- =====================================================================

-- Initial threshold config
INSERT INTO threshold_config (version, global_link_threshold, global_pending_threshold, active, activated_at)
VALUES ('v1.0_initial', 0.75, 0.60, TRUE, NOW())
ON CONFLICT (version) DO NOTHING;

-- Initial weights config
INSERT INTO weights_config (version, feature_weights, max_context_contribution, active, activated_at)
VALUES (
    'v2.0_production',
    '{"face": 6.0, "gait": 3.0, "plate": 2.0, "height_cat": 0.5, "build": 0.4, "hair_color": 0.3, "facial_hair": 0.1, "time_pattern": 0.3, "zone_pattern": 0.3, "plate_person_assoc": 0.4}'::JSON,
    1.0,
    TRUE,
    NOW()
)
ON CONFLICT (version) DO NOTHING;

-- =====================================================================
-- 8. Track identity persistence (for multi-frame tracking)
-- =====================================================================

CREATE TABLE IF NOT EXISTS track_identities (
    id SERIAL PRIMARY KEY,
    track_id VARCHAR(50) UNIQUE NOT NULL,
    customer_id INTEGER REFERENCES customers(id),
    confidence NUMERIC(5,3),

    -- Track metadata
    first_seen TIMESTAMP NOT NULL,
    last_seen TIMESTAMP NOT NULL,
    frame_count INTEGER NOT NULL DEFAULT 0,

    -- Voting history (JSON)
    customer_votes JSON,

    -- Evidence summary
    best_face_quality NUMERIC(3,2),
    best_gait_quality NUMERIC(3,2),
    has_plate BOOLEAN DEFAULT FALSE,

    -- Status
    status VARCHAR(20) NOT NULL DEFAULT 'active', -- 'active', 'linked', 'enrolled', 'expired'

    -- Timestamps
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_track_identities_customer ON track_identities(customer_id);
CREATE INDEX idx_track_identities_status ON track_identities(status);
CREATE INDEX idx_track_identities_last_seen ON track_identities(last_seen);

-- =====================================================================
-- 9. API helper views
-- =====================================================================

-- View: Customer with all biometric signals (for matching)
CREATE OR REPLACE VIEW customer_signals_summary AS
SELECT
    c.id AS customer_id,
    c.name,
    c.customer_number,
    COUNT(DISTINCT cf.id) AS face_count,
    COUNT(DISTINCT cg.id) AS gait_count,
    COUNT(DISTINCT cp.id) AS plate_count,
    MAX(cf.quality) AS best_face_quality,
    MAX(cg.quality) AS best_gait_quality,
    (
        SELECT json_agg(json_build_object(
            'height_category', height_category,
            'build', build,
            'hair_color', hair_color,
            'facial_hair', facial_hair,
            'confidence', confidence
        ))
        FROM customer_physical_attributes
        WHERE customer_id = c.id
        ORDER BY detected_at DESC
        LIMIT 1
    ) AS latest_physical_attrs
FROM customers c
LEFT JOIN customer_faces cf ON cf.customer_id = c.id
LEFT JOIN customer_gaits cg ON cg.customer_id = c.id
LEFT JOIN customer_plates cp ON cp.customer_id = c.id
GROUP BY c.id;

-- View: Recent decision audit summary
CREATE OR REPLACE VIEW decision_audit_summary AS
SELECT
    decision_type,
    COUNT(*) AS count,
    AVG(confidence) AS avg_confidence,
    AVG(biometric_ratio) AS avg_biometric_ratio,
    AVG(frame_count) AS avg_frame_count,
    threshold_version,
    weights_version,
    DATE(timestamp) AS decision_date
FROM decision_audit_log
GROUP BY decision_type, threshold_version, weights_version, DATE(timestamp)
ORDER BY decision_date DESC, decision_type;

-- =====================================================================
-- 10. Cleanup old expired tracks (run periodically)
-- =====================================================================

-- Function to cleanup expired tracks (older than 1 hour)
CREATE OR REPLACE FUNCTION cleanup_expired_tracks()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM track_identities
    WHERE status = 'active'
      AND last_seen < NOW() - INTERVAL '1 hour';

    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- DONE
-- =====================================================================
