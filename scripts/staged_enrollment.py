"""
Staged Customer Enrollment System

Problem: Same person generates varying quality embeddings (2.95 to 5.0 scores).
Low-quality frames immediately create duplicate customers.

Solution:
1. Detection → Pending Buffer (collect signals over 30s)
2. Aggregate best quality signals
3. Match against existing customers with relaxed threshold (3.0)
4. Only enroll as permanent customer if:
   - Best aggregated score >= 5.0 (high confidence new person)
   - OR no match found after 30s and score >= 3.0 (medium confidence)

This prevents:
- Immediate enrollment from poor quality frames
- Duplicate creation from same person at different angles
- False positives from partial signal matches
"""

import time
import numpy as np
import hashlib
from collections import defaultdict

class PendingCustomer:
    """Temporary customer buffer - aggregates signals before enrollment"""

    def __init__(self, track_id):
        self.track_id = track_id
        self.first_seen = time.time()
        self.last_seen = time.time()

        # Signal buffers - store all detections
        self.face_signals = []      # [(embedding_bytes, quality_score, timestamp)]
        self.gait_signals = []      # [(features_bytes, quality_score, timestamp)]
        self.physical_attrs = []    # [(attrs_dict, confidence, timestamp)]
        self.plate = None

        # Best aggregated score seen
        self.best_score = 0.0
        self.best_matched_customer_id = None

    def add_detection(self, face_bytes=None, gait_bytes=None, physical_attrs=None, plate=None):
        """Add new detection signals to buffer"""
        self.last_seen = time.time()

        if face_bytes:
            # Quality heuristic: embedding variance (higher = more distinctive = better quality)
            emb = np.frombuffer(face_bytes, dtype=np.float32)
            quality = float(np.var(emb))  # 0.0 to ~1.0
            self.face_signals.append((face_bytes, quality, time.time()))

        if gait_bytes:
            feat = np.frombuffer(gait_bytes, dtype=np.float32)
            quality = float(np.var(feat))
            self.gait_signals.append((gait_bytes, quality, time.time()))

        if physical_attrs:
            confidence = physical_attrs.get('confidence', 0.5)
            self.physical_attrs.append((physical_attrs, confidence, time.time()))

        if plate and not self.plate:
            self.plate = plate

    def get_best_signals(self):
        """Extract highest quality signals from buffer"""
        best_face = None
        best_gait = None
        best_physical = None

        if self.face_signals:
            # Sort by quality, take best
            best_face = max(self.face_signals, key=lambda x: x[1])[0]

        if self.gait_signals:
            best_gait = max(self.gait_signals, key=lambda x: x[1])[0]

        if self.physical_attrs:
            best_physical = max(self.physical_attrs, key=lambda x: x[1])[0]

        return best_face, best_gait, best_physical, self.plate

    def signal_count(self):
        """Count unique signal types available"""
        count = 0
        if self.face_signals: count += 1
        if self.gait_signals: count += 1
        if self.plate: count += 1
        return count

    def age(self):
        """How long since first seen (seconds)"""
        return time.time() - self.first_seen

    def idle_time(self):
        """How long since last detection (seconds)"""
        return time.time() - self.last_seen


class StagedEnrollmentManager:
    """Manages pending customers and staged enrollment"""

    def __init__(self):
        self.pending = {}  # track_id -> PendingCustomer
        self.lock = threading.Lock()

    def generate_track_id(self, face_bytes, gait_bytes):
        """Generate stable track ID from biometric signals"""
        # Use hash of concatenated embeddings as track ID
        data = b''
        if face_bytes:
            data += face_bytes[:128]  # First 128 bytes of face
        if gait_bytes:
            data += gait_bytes

        if not data:
            # Fallback: random ID
            import uuid
            return str(uuid.uuid4())

        return hashlib.md5(data).hexdigest()[:16]

    def add_detection(self, face_bytes=None, gait_bytes=None, physical_attrs=None, plate=None):
        """
        Add new detection to pending buffer.
        Returns: track_id
        """
        with self.lock:
            track_id = self.generate_track_id(face_bytes, gait_bytes)

            if track_id not in self.pending:
                self.pending[track_id] = PendingCustomer(track_id)

            self.pending[track_id].add_detection(face_bytes, gait_bytes, physical_attrs, plate)

            return track_id

    def get_ready_for_enrollment(self, identify_func):
        """
        Check which pending customers are ready for permanent enrollment.

        Returns: [(track_id, PendingCustomer, matched_customer_id, score)]

        Ready conditions:
        1. Best score >= 5.0 (high confidence new person) - enroll immediately
        2. Age >= 30s AND score >= 3.0 (medium confidence) - enroll after timeout
        3. Age >= 30s AND no match found - enroll with available signals
        4. Idle > 10s - person left, process what we have
        """
        ready = []

        with self.lock:
            now = time.time()

            for track_id, pending in list(self.pending.items()):
                # Clean up old expired tracks (>60s idle)
                if pending.idle_time() > 60:
                    del self.pending[track_id]
                    continue

                # Get best signals
                face_bytes, gait_bytes, physical_attrs, plate = pending.get_best_signals()

                # Try to match against existing customers
                customer_id, score, features = identify_func(
                    plate=plate,
                    face_bytes=face_bytes,
                    gait_bytes=gait_bytes,
                    physical_attrs=physical_attrs
                )

                pending.best_score = max(pending.best_score, score)
                if customer_id:
                    pending.best_matched_customer_id = customer_id

                # Decision logic
                age = pending.age()
                idle = pending.idle_time()
                signal_count = pending.signal_count()

                # Condition 1: Strong match found (customer exists)
                if customer_id and score >= 3.0:
                    ready.append((track_id, pending, customer_id, score, 'matched_existing'))
                    continue

                # Condition 2: High confidence new person
                if score >= 5.0 and signal_count >= 2:
                    ready.append((track_id, pending, None, score, 'high_confidence_new'))
                    continue

                # Condition 3: Timeout - medium confidence
                if age >= 30 and score >= 3.0 and signal_count >= 2:
                    ready.append((track_id, pending, None, score, 'timeout_medium_confidence'))
                    continue

                # Condition 4: Person left (idle), process what we have
                if idle >= 10 and signal_count >= 2 and score >= 2.5:
                    ready.append((track_id, pending, None, score, 'left_scene'))
                    continue

            # Remove processed tracks
            for track_id, _, _, _, _ in ready:
                if track_id in self.pending:
                    del self.pending[track_id]

        return ready

    def cleanup(self):
        """Remove expired pending customers"""
        with self.lock:
            now = time.time()
            expired = [tid for tid, p in self.pending.items() if p.idle_time() > 60]
            for tid in expired:
                del self.pending[tid]

    def stats(self):
        """Get statistics about pending customers"""
        with self.lock:
            return {
                'pending_count': len(self.pending),
                'oldest_age': max([p.age() for p in self.pending.values()], default=0),
                'total_signals': sum([p.signal_count() for p in self.pending.values()], default=0)
            }
