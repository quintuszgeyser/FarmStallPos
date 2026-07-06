import json as _json
import base64
import statistics as _stats
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from flask import Blueprint, jsonify, request, Response
from sqlalchemy import text

from helpers import require_login, require_role, current_user, get_setting
from models import (
    db,
    Customer, CustomerPlate, CustomerFace, CustomerGait,
    CustomerVisit, PlateDetection, Sale,
)

bp = Blueprint('customers', __name__)

# Recognition service URL - imported lazily to avoid circular
def _recog_url():
    import os
    return os.environ.get('RECOGNITION_URL', 'http://farmpos-recognition:8080')

_ATTR_WINDOW = 10


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _customer_dict(c, _extra=None):
    if _extra:
        has_face, has_gait, has_photo, has_body, hr_avg, hr_std, plates_str = _extra
        plates = plates_str.split(',') if plates_str else []
    else:
        row = db.session.execute(text('''
            SELECT
              (SELECT COUNT(*) FROM customer_faces WHERE customer_id=:cid AND active=TRUE) > 0,
              (SELECT COUNT(*) FROM customer_gaits WHERE customer_id=:cid AND active=TRUE) > 0,
              (SELECT COUNT(*) FROM customer_faces WHERE customer_id=:cid AND photo IS NOT NULL) > 0,
              (SELECT COUNT(*) FROM customer_faces WHERE customer_id=:cid AND body_photo IS NOT NULL) > 0,
              (SELECT AVG(EXTRACT(HOUR FROM detected_at)) FROM customer_visits WHERE customer_id=:cid),
              (SELECT STDDEV(EXTRACT(HOUR FROM detected_at)) FROM customer_visits WHERE customer_id=:cid),
              (SELECT STRING_AGG(plate_number,',') FROM customer_plates WHERE customer_id=:cid AND active=TRUE)
        '''), {'cid': c.id}).fetchone()
        has_face, has_gait, has_photo, has_body = row[0], row[1], row[2], row[3]
        hr_avg, hr_std = row[4], row[5]
        plates = row[6].split(',') if row[6] else []
    return {
        'id': c.id, 'name': c.name, 'phone': c.phone, 'email': c.email,
        'notes': c.notes, 'visit_count': c.visit_count, 'active': c.active,
        'enrolled_at': c.enrolled_at.isoformat() if c.enrolled_at else None,
        'last_visit': c.last_visit.isoformat() if c.last_visit else None,
        'customer_number': c.customer_number,
        'auto_enrolled': c.auto_enrolled,
        'first_seen': c.first_seen.isoformat() if c.first_seen else None,
        'is_employee': c.is_employee,
        'merged_into': c.merged_into,
        'is_online_customer': c.is_online_customer,
        'is_pos_customer': c.is_pos_customer,
        'plates': plates,
        'has_face': bool(has_face), 'has_gait': bool(has_gait),
        'has_photo': bool(has_photo), 'has_body_photo': bool(has_body),
        'visit_hour_avg': float(hr_avg) if hr_avg is not None else None,
        'visit_hour_std': float(hr_std) if hr_std is not None else None,
    }


def _build_customer_list(customers):
    if not customers:
        return []
    cids = [c.id for c in customers]
    rows = db.session.execute(text('''
        SELECT c.id,
          (SELECT COUNT(*) > 0 FROM customer_faces WHERE customer_id=c.id AND active=TRUE),
          (SELECT COUNT(*) > 0 FROM customer_gaits WHERE customer_id=c.id AND active=TRUE),
          (SELECT COUNT(*) > 0 FROM customer_faces WHERE customer_id=c.id AND photo IS NOT NULL),
          (SELECT COUNT(*) > 0 FROM customer_faces WHERE customer_id=c.id AND body_photo IS NOT NULL),
          (SELECT AVG(EXTRACT(HOUR FROM detected_at)) FROM customer_visits WHERE customer_id=c.id),
          (SELECT STDDEV(EXTRACT(HOUR FROM detected_at)) FROM customer_visits WHERE customer_id=c.id),
          (SELECT STRING_AGG(plate_number, ',') FROM customer_plates WHERE customer_id=c.id AND active=TRUE)
        FROM customers c WHERE c.id = ANY(:cids)
    '''), {'cids': cids}).fetchall()
    extras = {row[0]: row[1:] for row in rows}

    # Bulk spend stats - receipt-level to avoid row inflation
    spend_rows = db.session.execute(text('''
        WITH receipts AS (
            SELECT s.customer_id, s.sale_id,
                SUM(s.qty * s.unit_price) AS receipt_total
            FROM sales s
            WHERE s.customer_id = ANY(:cids) AND COALESCE(s.voided, FALSE) = FALSE
            GROUP BY s.customer_id, s.sale_id
        )
        SELECT customer_id,
            COUNT(*)                      AS receipt_count,
            COALESCE(SUM(receipt_total),0) AS total_spent,
            CASE WHEN COUNT(*) > 0 THEN COALESCE(SUM(receipt_total),0)/COUNT(*) ELSE 0 END AS avg_basket
        FROM receipts
        GROUP BY customer_id
    '''), {'cids': cids}).fetchall()
    spend_by_cid = {r[0]: {'receipt_count': int(r[1]), 'total_spent': float(r[2]), 'avg_basket': round(float(r[3]),2)} for r in spend_rows}

    attr_rows = db.session.execute(text('''
        SELECT customer_id, height_cm, hair_color, skin_tone, build, eye_color,
               age_range, gender, wearing_glasses, facial_hair, detected_at,
               camera_source, confidence, height_category
        FROM customer_physical_attributes
        WHERE customer_id = ANY(:cids)
        ORDER BY customer_id, detected_at DESC
    '''), {'cids': cids}).fetchall()

    attr_by_cid = defaultdict(list)
    for r in attr_rows:
        attr_by_cid[r[0]].append(r[1:])

    def _quick_vote(rows):
        if not rows: return None
        def mode_of(vals):
            counts = Counter(v for v in vals if v is not None and v != '')
            return counts.most_common(1)[0][0] if counts else None
        return {'gender': mode_of([r[6] for r in rows]), 'build': mode_of([r[3] for r in rows]),
                'hair_color': mode_of([r[1] for r in rows]), 'age_range': mode_of([r[5] for r in rows])}

    voted_attrs = {cid: _quick_vote(attr_by_cid[cid]) for cid in cids}
    result = []
    for c in customers:
        d = _customer_dict(c, extras.get(c.id))
        d['physical_attributes'] = voted_attrs.get(c.id)
        spend = spend_by_cid.get(c.id, {})
        d['total_spent']     = spend.get('total_spent', 0.0)
        d['avg_basket']      = spend.get('avg_basket', 0.0)
        d['receipt_count']   = spend.get('receipt_count', 0)
        result.append(d)
    return result


def _fetch_attr_rows(cid, limit=None):
    lim = limit if limit is not None else _ATTR_WINDOW
    return db.session.execute(
        text("""SELECT height_cm, hair_color, skin_tone, build, eye_color,
                       age_range, gender, wearing_glasses, facial_hair,
                       detected_at, camera_source, confidence, height_category
                FROM customer_physical_attributes
                WHERE customer_id = :cid ORDER BY detected_at DESC LIMIT :lim"""),
        {'cid': cid, 'lim': lim}
    ).fetchall()


def _voted_attributes(rows):
    if not rows: return None
    def mode_of(vals):
        counts = Counter(v for v in vals if v is not None and v != '')
        return counts.most_common(1)[0][0] if counts else None
    def bool_vote(vals):
        nn = [v for v in vals if v is not None]
        return sum(1 for v in nn if v) > len(nn) / 2 if nn else None
    def median_int(vals):
        nums = sorted(v for v in vals if v is not None)
        return nums[len(nums) // 2] if nums else None
    return {
        'height_cm': median_int([r[0] for r in rows]), 'hair_color': mode_of([r[1] for r in rows]),
        'skin_tone': mode_of([r[2] for r in rows]), 'build': mode_of([r[3] for r in rows]),
        'eye_color': mode_of([r[4] for r in rows]), 'age_range': mode_of([r[5] for r in rows]),
        'gender': mode_of([r[6] for r in rows]), 'wearing_glasses': bool_vote([r[7] for r in rows]),
        'facial_hair': mode_of([r[8] for r in rows]),
        'detected_at': rows[0][9].isoformat() if rows[0][9] else None,
        'camera_source': rows[0][10],
        'confidence': float(rows[0][11]) if rows[0][11] else None,
        'height_category': mode_of([r[12] for r in rows]),
    }


def _merge_primary_score(row):
    score = (row[2] or 0) * 10
    if row[1]: score += 100
    if row[3]: score += 50
    if row[4]: score += 30
    if row[5]:
        try:
            age_days = (datetime.utcnow() - row[5].replace(tzinfo=None)).days
            score += min(age_days, 365)
        except Exception: pass
    return score


def _recompute_customer_embeddings(customer_id, _text_fn):
    import numpy as _np
    MAX_EMBEDDINGS = int(float(get_setting('max_face_angles', 24) or 24))
    MIN_DISTANCE   = float(get_setting('min_angle_distance', 0.25) or 0.25)
    face_rows = db.session.execute(_text_fn('''
        SELECT id, embedding, photo, original_customer_id FROM customer_faces
        WHERE customer_id = :cid AND active = TRUE
    '''), {'cid': customer_id}).fetchall()
    if not face_rows: return 0
    normed = []
    for row in face_rows:
        emb = _np.frombuffer(row[1], dtype=_np.float32)
        if emb.shape == (512,):
            n = _np.linalg.norm(emb)
            if n > 0: normed.append((emb / n, bytes(emb.tobytes()), row[2], row[3]))
    selected = []
    for normed_emb, raw_bytes, photo, orig_cid in normed:
        is_new = all(float(_np.dot(normed_emb, s[0])) < (1.0 - MIN_DISTANCE) for s in selected)
        if is_new: selected.append((normed_emb, raw_bytes, photo, orig_cid))
        if len(selected) >= MAX_EMBEDDINGS: break
    db.session.execute(_text_fn('UPDATE customer_faces SET active = FALSE WHERE customer_id = :cid'), {'cid': customer_id})
    for _, raw_bytes, photo, orig_cid in selected:
        db.session.execute(_text_fn('''
            INSERT INTO customer_faces (customer_id, embedding, photo, enrolled_at, active, original_customer_id)
            VALUES (:cid, :emb, :photo, NOW(), TRUE, :orig)
        '''), {'cid': customer_id, 'emb': raw_bytes, 'photo': photo, 'orig': orig_cid})
    return len(selected)


# ---------------------------------------------------------------------------
# Customer CRUD
# ---------------------------------------------------------------------------

@bp.route('/api/customers', methods=['GET'])
def api_customers_get():
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(_build_customer_list(Customer.query.filter_by(active=True).order_by(Customer.name.asc()).all()))


@bp.route('/api/customers/<int:cid>', methods=['GET'])
def api_customer_get_single(cid):
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    c = db.session.get(Customer, cid)
    if not c: return jsonify({'error': 'Not found'}), 404
    return jsonify(_customer_dict(c))


@bp.route('/api/customers', methods=['POST'])
def api_customers_post():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    data    = request.json or {}
    name    = (data.get('name') or '').strip() or None
    is_online = bool(data.get('is_online_customer', False))
    is_pos    = bool(data.get('is_pos_customer', not is_online))
    u = current_user()
    c = Customer(
        name=name,
        phone=(data.get('phone') or '').strip() or None,
        email=(data.get('email') or '').strip() or None,
        notes=(data.get('notes') or '').strip() or None,
        enrolled_by=u.id if u else None,
        auto_enrolled=data.get('auto_enrolled', False),
        customer_number=data.get('customer_number'),
        first_seen=datetime.fromisoformat(data['first_seen']) if data.get('first_seen') else None,
        is_online_customer=is_online,
        is_pos_customer=is_pos,
    )
    db.session.add(c)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': 'customer_number_conflict'}), 409
        raise
    return jsonify({'ok': True, 'id': c.id})


@bp.route('/api/customers/<int:cid>', methods=['POST'])
def api_customers_update(cid):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c: return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    if 'name'               in data: c.name               = (data['name'] or '').strip() or None
    if 'phone'              in data: c.phone              = (data['phone'] or '').strip() or None
    if 'email'              in data: c.email              = (data['email'] or '').strip() or None
    if 'notes'              in data: c.notes              = (data['notes'] or '').strip() or None
    if 'active'             in data: c.active             = bool(data['active'])
    if 'is_employee'        in data: c.is_employee        = bool(data['is_employee'])
    if 'is_online_customer' in data: c.is_online_customer = bool(data['is_online_customer'])
    if 'is_pos_customer'    in data: c.is_pos_customer    = bool(data['is_pos_customer'])
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/customers/<int:cid>', methods=['DELETE'])
def api_customers_delete(cid):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c: return jsonify({'error': 'Not found'}), 404
    c.active = False
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/customers/cleanup_empty', methods=['POST'])
def api_customers_cleanup_empty():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    cutoff = datetime.utcnow() - timedelta(days=30)
    rows = db.session.execute(text('''
        SELECT c.id FROM customers c
        WHERE c.auto_enrolled = TRUE AND c.name IS NULL AND c.active = TRUE
          AND NOT EXISTS (SELECT 1 FROM customer_faces WHERE customer_id=c.id AND active=TRUE)
          AND (c.last_visit IS NULL OR c.last_visit < :cutoff)
    '''), {'cutoff': cutoff}).fetchall()
    ids = [r[0] for r in rows]
    if not ids: return jsonify({'ok': True, 'deleted': 0})
    # Use parameterised ANY(:ids) to avoid raw f-string SQL injection risk
    id_array = ids  # list of Python ints from the SELECT above
    db.session.execute(
        text('DELETE FROM customer_exclusions WHERE customer_id_a = ANY(:ids) OR customer_id_b = ANY(:ids)'),
        {'ids': id_array},
    )
    db.session.execute(
        text('DELETE FROM customer_merge_log WHERE source_id = ANY(:ids) OR primary_id = ANY(:ids)'),
        {'ids': id_array},
    )
    for cid in ids:
        c = db.session.get(Customer, cid)
        if c: db.session.delete(c)
    db.session.commit()
    return jsonify({'ok': True, 'deleted': len(ids)})


@bp.route('/api/customers/<int:cid>/delete_permanent', methods=['POST'])
def api_customers_delete_permanent(cid):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    try:
        # 1) delete the customer's own child detail rows
        for tbl in ['customer_physical_attributes', 'customer_faces', 'customer_gaits',
                    'customer_visits', 'customer_plates', 'visit_sessions',
                    'till_detections', 'customer_signal_history']:
            db.session.execute(text(f'DELETE FROM {tbl} WHERE customer_id = :cid'), {'cid': cid})
        # 2) clear merge-provenance back-refs left on the PRIMARY's rows by a prior merge
        #    (these reference the now-deleted source via original_customer_id)
        for tbl in ['customer_faces', 'customer_gaits', 'customer_physical_attributes']:
            db.session.execute(text(f'UPDATE {tbl} SET original_customer_id = NULL WHERE original_customer_id = :cid'), {'cid': cid})
        # 3) delete relationship rows that reference this customer
        db.session.execute(text('DELETE FROM customer_merge_log WHERE source_id = :cid OR primary_id = :cid'), {'cid': cid})
        db.session.execute(text('DELETE FROM customer_exclusions WHERE customer_id_a = :cid OR customer_id_b = :cid'), {'cid': cid})
        db.session.execute(text('DELETE FROM customer_conflicts WHERE customer_id_a = :cid OR customer_id_b = :cid OR merged_into = :cid'), {'cid': cid})
        # 4) unlink references on records we keep (transactions / tracks / plates)
        db.session.execute(text('UPDATE person_tracks    SET customer_id = NULL WHERE customer_id = :cid'), {'cid': cid})
        db.session.execute(text('UPDATE plate_detections SET customer_id = NULL WHERE customer_id = :cid'), {'cid': cid})
        db.session.execute(text('UPDATE sales            SET customer_id = NULL WHERE customer_id = :cid'), {'cid': cid})
        db.session.execute(text('UPDATE invoices         SET customer_id = NULL WHERE customer_id = :cid'), {'cid': cid})
        db.session.execute(text('UPDATE customers        SET merged_into = NULL WHERE merged_into = :cid'), {'cid': cid})
        # 5) finally remove the customer
        db.session.execute(text('DELETE FROM customers WHERE id = :cid'), {'cid': cid})
        db.session.commit()
        try:
            import requests as _req
            _req.post(f'{_recog_url()}/control/purge_customer', json={'customer_id': cid}, timeout=3)
        except Exception: pass
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/customers/<int:cid>/name', methods=['POST'])
def api_customer_name(cid):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c: return jsonify({'error': 'Not found'}), 404
    name = (request.json or {}).get('name', '').strip()
    if not name: return jsonify({'error': 'name required'}), 400
    c.name = name
    db.session.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Profile & analytics
# ---------------------------------------------------------------------------

_DOW_NAMES = {0:'Sunday',1:'Monday',2:'Tuesday',3:'Wednesday',4:'Thursday',5:'Friday',6:'Saturday'}

@bp.route('/api/customers/<int:cid>/profile', methods=['GET'])
def api_customer_profile(cid):
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    customer = db.session.get(Customer, cid)
    if not customer: return jsonify({'error': 'Customer not found'}), 404

    # ── Receipt-level CTE - one row per receipt, avoids row-based inflation ──
    receipt_rows = db.session.execute(text("""
        WITH receipt_sales AS (
            SELECT
                s.sale_id,
                MIN(s.date_time)                                              AS sale_at,
                SUM(s.qty * s.unit_price)                                     AS receipt_total,
                MAX(CASE WHEN oo.id IS NOT NULL THEN 1 ELSE 0 END)            AS is_online
            FROM sales s
            LEFT JOIN online_orders oo ON oo.pos_sale_id::text = s.sale_id
            WHERE s.customer_id = :cid
              AND COALESCE(s.voided, FALSE) = FALSE
            GROUP BY s.sale_id
        ),
        purchase_gaps AS (
            SELECT sale_at,
                   LAG(sale_at) OVER (ORDER BY sale_at) AS prev_sale_at
            FROM receipt_sales
        ),
        visit_gaps AS (
            SELECT detected_at,
                   LAG(detected_at) OVER (ORDER BY detected_at) AS prev_detected_at
            FROM customer_visits
            WHERE customer_id = :cid
        ),
        fav_day AS (
            SELECT EXTRACT(DOW FROM sale_at)::int AS dow, COUNT(*) AS n
            FROM receipt_sales
            GROUP BY 1 ORDER BY n DESC, 1 LIMIT 1
        ),
        fav_hour AS (
            SELECT CASE
                WHEN EXTRACT(HOUR FROM sale_at) < 12 THEN 'Morning'
                WHEN EXTRACT(HOUR FROM sale_at) < 17 THEN 'Afternoon'
                ELSE 'Evening'
            END AS bucket, COUNT(*) AS n
            FROM receipt_sales
            GROUP BY 1 ORDER BY n DESC, 1 LIMIT 1
        )
        SELECT
            COUNT(*)                                                           AS receipt_count_total,
            COUNT(*) FILTER (WHERE is_online = 1)                             AS online_receipt_count,
            COUNT(*) FILTER (WHERE is_online = 0)                             AS instore_receipt_count,
            COALESCE(SUM(receipt_total), 0)                                   AS total_spent,
            COALESCE(SUM(receipt_total) FILTER (WHERE is_online = 1), 0)      AS online_spend,
            COALESCE(SUM(receipt_total) FILTER (WHERE is_online = 0), 0)      AS instore_spend,
            CASE WHEN COUNT(*) > 0
                 THEN COALESCE(SUM(receipt_total),0) / COUNT(*) ELSE 0 END    AS avg_basket,
            MAX(sale_at)                                                       AS last_purchase_at,
            CASE WHEN MAX(sale_at) IS NOT NULL
                 THEN (CURRENT_DATE - DATE(MAX(sale_at))) ELSE NULL END        AS days_since_purchase,
            (SELECT MAX(DATE_PART('day', sale_at - prev_sale_at))
             FROM purchase_gaps WHERE prev_sale_at IS NOT NULL)                AS longest_purchase_gap_days,
            (SELECT MAX(DATE_PART('day', detected_at - prev_detected_at))
             FROM visit_gaps WHERE prev_detected_at IS NOT NULL)               AS longest_visit_gap_days,
            (SELECT AVG(DATE_PART('day', sale_at - prev_sale_at))
             FROM purchase_gaps WHERE prev_sale_at IS NOT NULL)                AS avg_days_between_purchases,
            (SELECT dow FROM fav_day)                                          AS fav_dow,
            (SELECT bucket FROM fav_hour)                                      AS fav_time_bucket
        FROM receipt_sales
    """), {'cid': cid}).fetchone()

    r = receipt_rows
    total_spent   = float(r.total_spent or 0)
    online_spend  = float(r.online_spend or 0)
    instore_spend = float(r.instore_spend or 0)

    # ── Build receipts dict for purchase history (using ORM for item details) ──
    sales = Sale.query.filter(Sale.customer_id == cid, Sale.voided == False).order_by(Sale.date_time.desc()).all()
    receipts, product_counts = {}, {}
    for sale in sales:
        if sale.sale_id not in receipts:
            receipts[sale.sale_id] = {'sale_id': sale.sale_id, 'date_time': sale.date_time.isoformat(), 'total': Decimal('0'), 'items': []}
        item_total = sale.qty * sale.unit_price
        receipts[sale.sale_id]['total'] += item_total
        receipts[sale.sale_id]['items'].append({'product_id': sale.product_id, 'product_name': sale.product.name if sale.product else 'Unknown', 'qty': float(sale.qty), 'unit_price': float(sale.unit_price)})
        pid = sale.product_id
        if pid not in product_counts:
            product_counts[pid] = {'product_id': pid, 'name': sale.product.name if sale.product else 'Unknown', 'count': 0, 'total_spent': Decimal('0')}
        product_counts[pid]['count'] += 1
        product_counts[pid]['total_spent'] += item_total

    top_products = sorted(product_counts.values(), key=lambda x: x['count'], reverse=True)[:10]
    for p in top_products: p['total_spent'] = float(p['total_spent'])

    sessions_result = db.session.execute(text("""SELECT session_start, session_end, dwell_seconds, purchase_made, sale_ids FROM visit_sessions WHERE customer_id = :cid ORDER BY session_start DESC LIMIT 20"""), {'cid': cid}).fetchall()
    sessions   = [{'session_start': row[0].isoformat() if row[0] else None, 'session_end': row[1].isoformat() if row[1] else None, 'dwell_seconds': row[2], 'purchase_made': row[3], 'sale_ids': row[4]} for row in sessions_result]
    total_dwell = sum(row[2] for row in sessions_result if row[2])
    avg_dwell   = total_dwell / len(sessions) if sessions else 0

    plates        = [p.plate_number for p in customer.plates if p.active]
    face_enrolled = any(f.active for f in customer.faces)
    gait_enrolled = any(g.active for g in customer.gaits)

    return jsonify({
        'customer_id': cid, 'customer_number': customer.customer_number,
        'name': customer.name, 'phone': customer.phone, 'email': customer.email,
        'auto_enrolled': customer.auto_enrolled,
        'first_seen':  customer.first_seen.isoformat()  if customer.first_seen  else None,
        'last_visit':  customer.last_visit.isoformat()   if customer.last_visit  else None,
        'visit_count': customer.visit_count,
        # ── Core spend (receipt-accurate) ──
        'total_spent':           total_spent,
        'avg_basket':            float(r.avg_basket or 0),
        'receipt_count_total':   int(r.receipt_count_total or 0),
        # ── Channel split ──
        'online_count':          int(r.online_receipt_count or 0),
        'instore_count':         int(r.instore_receipt_count or 0),
        'online_spend':          round(online_spend, 2),
        'instore_spend':         round(instore_spend, 2),
        'online_spend_pct':      round(online_spend / total_spent * 100, 1) if total_spent else None,
        # ── Behaviour ──
        'fav_day':               _DOW_NAMES.get(int(r.fav_dow)) if r.fav_dow is not None else None,
        'fav_time':              r.fav_time_bucket,
        'days_since_purchase':   int(r.days_since_purchase)          if r.days_since_purchase          is not None else None,
        'longest_gap_days':      int(r.longest_visit_gap_days)        if r.longest_visit_gap_days        is not None else None,
        'longest_purchase_gap':  int(r.longest_purchase_gap_days)    if r.longest_purchase_gap_days    is not None else None,
        'avg_days_between_purchases': round(float(r.avg_days_between_purchases), 1) if r.avg_days_between_purchases is not None else None,
        # ── Existing ──
        'avg_dwell_seconds': avg_dwell,
        'receipts':     [{**rec, 'total': float(rec['total'])} for rec in receipts.values()],
        'top_products': top_products,
        'recent_sessions': sessions,
        'signals':      {'plates': plates, 'face_enrolled': face_enrolled, 'gait_enrolled': gait_enrolled},
    })


@bp.route('/api/customers/<int:cid>/radar', methods=['GET'])
def api_customer_radar(cid):
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    c = db.session.get(Customer, cid)
    if not c: return jsonify({'error': 'Not found'}), 404

    face_angles    = CustomerFace.query.filter_by(customer_id=cid, active=True).count()
    identity_score = min(1.0, face_angles / 10.0)

    sim_rows = db.session.execute(text("SELECT confidence_scores FROM customer_visits WHERE customer_id=:cid AND confidence_scores IS NOT NULL ORDER BY detected_at DESC LIMIT 20"), {'cid': cid}).fetchall()
    face_sims = []
    for (s,) in sim_rows:
        try:
            sc = _json.loads(s)
            sim = float(sc.get('face_similarity', 0) or 0)
            if sim > 0: face_sims.append(sim)
        except Exception: pass
    if face_sims:
        avg_sim = sum(face_sims) / len(face_sims)
        variance = _stats.variance(face_sims) if len(face_sims) > 1 else 0.0
        stability_score = min(1.0, avg_sim * (1.0 - min(1.0, variance * 5)))
    else:
        stability_score = 0.0
    best_sim = max(face_sims) if face_sims else 0.0

    last_visit_days = None; recency = 0.0
    if c.last_visit:
        last_visit_days = (datetime.utcnow() - c.last_visit).days
        recency = max(0.0, 1.0 - last_visit_days / 30.0)
    latest_face = CustomerFace.query.filter_by(customer_id=cid, active=True).order_by(CustomerFace.enrolled_at.desc()).first()
    emb_age_days = (datetime.utcnow() - latest_face.enrolled_at).days if latest_face else 999
    emb_freshness = max(0.0, 1.0 - emb_age_days / 14.0)
    freshness_score = recency * 0.6 + emb_freshness * 0.4

    purchase_receipts = db.session.execute(text("SELECT COUNT(DISTINCT sale_id) FROM sales WHERE customer_id=:cid AND voided=FALSE"), {'cid': cid}).scalar() or 0
    conversion_score = 0.0 if purchase_receipts == 0 else 0.33 if purchase_receipts == 1 else 0.66 if purchase_receipts <= 3 else 1.0

    product_variety = db.session.execute(text("SELECT COUNT(DISTINCT product_id) FROM sales WHERE customer_id=:cid AND voided=FALSE"), {'cid': cid}).scalar() or 0
    if purchase_receipts == 0: basket_score = 0.0
    elif purchase_receipts == 1: basket_score = 0.30
    elif purchase_receipts <= 4: basket_score = 0.60
    else: basket_score = min(1.0, 0.60 + (product_variety / 10.0) * 0.4)

    plate_count = CustomerPlate.query.filter_by(customer_id=cid, active=True).count()
    if plate_count == 0: plate_score = 0.0
    else:
        plate_visits = db.session.execute(text("SELECT COUNT(*) FROM customer_visits WHERE customer_id=:cid AND matched_signals LIKE '%plate%'"), {'cid': cid}).scalar() or 0
        plate_score = 0.4 if plate_visits == 0 else 0.7 if plate_visits <= 2 else 1.0

    distinct_days = db.session.execute(text("SELECT COUNT(DISTINCT DATE(detected_at)) FROM customer_visits WHERE customer_id=:cid"), {'cid': cid}).scalar() or 0
    visit_count = c.visit_count or 0
    if distinct_days == 0: regularity_score = 0.0
    else:
        day_spread_score = min(1.0, distinct_days / 10.0)
        visits_per_day   = visit_count / distinct_days
        regularity_score = min(1.0, day_spread_score + min(0.2, visits_per_day / 10.0))

    voted_attrs = _voted_attributes(_fetch_attr_rows(cid))
    attr_fields  = [voted_attrs[k] for k in ('hair_color','build','height_category','age_range','gender','skin_tone','eye_color','facial_hair','wearing_glasses')] if voted_attrs else []
    attrs_filled = sum(1 for v in attr_fields if v is not None and v != '' and v is not False)
    attrs_total  = len(attr_fields) or 9
    has_gait  = CustomerGait.query.filter_by(customer_id=cid, active=True).count() > 0
    has_photo = CustomerFace.query.filter_by(customer_id=cid).filter(CustomerFace.photo != None).count() > 0
    is_named  = bool(c.name and c.name.strip())
    depth_score = min(1.0, (face_angles / 10.0) * 0.25 + (attrs_filled / attrs_total) * 0.20 + (1.0 if has_gait else 0.0) * 0.15 + (1.0 if has_photo else 0.0) * 0.15 + (1.0 if is_named else 0.0) * 0.15 + (1.0 if plate_count > 0 else 0.0) * 0.10)

    return jsonify({'customer_id': cid, 'name': c.name or c.customer_number,
        'biometric': {'Identity': identity_score, 'Stability': stability_score, 'Freshness': freshness_score, 'Attributes': attrs_filled / attrs_total, 'Gait': 1.0 if has_gait else 0.0, 'Photo': 1.0 if has_photo else 0.0, 'Named': 1.0 if is_named else 0.0, 'Plate conf': plate_score},
        'behavioural': {'Conversion': conversion_score, 'Basket': basket_score, 'Regularity': regularity_score, 'Depth': depth_score, 'Plate': plate_score, 'Purchases': min(1.0, purchase_receipts / 10.0), 'Days active': min(1.0, distinct_days / 14.0), 'Recency': recency},
        'scores': {'Identity': identity_score, 'Stability': stability_score, 'Freshness': freshness_score, 'Conversion': conversion_score, 'Basket': basket_score, 'Plate': plate_score, 'Regularity': regularity_score, 'Depth': depth_score},
        'details': {'face_angles': face_angles, 'best_face_sim': round(best_sim * 100, 1), 'avg_face_sim': round(sum(face_sims) / len(face_sims) * 100 if face_sims else 0, 1), 'attrs_filled': attrs_filled, 'attrs_total': attrs_total, 'plate_count': plate_count, 'is_named': is_named, 'purchase_count': purchase_receipts, 'product_variety': product_variety, 'visit_count': visit_count, 'distinct_days': distinct_days, 'last_visit_days': last_visit_days, 'emb_age_days': emb_age_days if emb_age_days < 999 else None}})


@bp.route('/api/customers/<int:cid>/visits', methods=['GET'])
def api_customer_visits(cid):
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    visits = CustomerVisit.query.filter_by(customer_id=cid).order_by(CustomerVisit.detected_at.desc()).limit(20).all()
    result = []
    for v in visits:
        scores = {}
        try: scores = _json.loads(v.confidence_scores) if v.confidence_scores else {}
        except Exception: pass
        result.append({'id': v.id, 'detected_at': v.detected_at.isoformat(), 'matched_signals': v.matched_signals, 'confidence_scores': scores, 'camera_source': v.camera_source})
    return jsonify(result)


@bp.route('/api/customers/<int:cid>/sales', methods=['GET'])
def api_customer_sales(cid):
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    start = request.args.get('start'); end = request.args.get('end')
    q = Sale.query.filter(Sale.customer_id == cid, Sale.voided == False)
    if start: q = q.filter(Sale.date_time >= datetime.fromisoformat(start))
    if end:   q = q.filter(Sale.date_time <= datetime.fromisoformat(end))
    return jsonify([{'sale_id': s.sale_id, 'date_time': s.date_time.isoformat(), 'product_id': s.product_id, 'qty': float(s.qty), 'unit_price': float(s.unit_price)} for s in q.all()])


# ---------------------------------------------------------------------------
# Merge / exclusions
# ---------------------------------------------------------------------------

@bp.route('/api/customers/merge_suggestions', methods=['GET'])
def api_customers_merge_suggestions():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    import numpy as np
    min_sim   = float(get_setting('merge_suggest_min_sim', 0.75) or 0.75)
    customers = Customer.query.filter_by(active=True).all()
    embeddings = {}
    for c in customers:
        if c.is_employee: continue
        rows = CustomerFace.query.filter_by(customer_id=c.id, active=True).all()
        embs = []
        for row in rows:
            if row.embedding and len(row.embedding) == 2048:
                emb = np.frombuffer(row.embedding, dtype=np.float32)
                n   = np.linalg.norm(emb)
                if n > 0: embs.append(emb / n)
        if embs: embeddings[c.id] = (embs, c)
    excl_rows = db.session.execute(db.text("SELECT customer_id_a, customer_id_b FROM customer_exclusions")).fetchall()
    excluded  = {(min(r[0], r[1]), max(r[0], r[1])) for r in excl_rows}
    cids = list(embeddings.keys())
    suggestions = []
    for i in range(len(cids)):
        for j in range(i + 1, len(cids)):
            a_id, b_id = cids[i], cids[j]
            if (min(a_id, b_id), max(a_id, b_id)) in excluded: continue
            a_embs, a_c = embeddings[a_id]; b_embs, b_c = embeddings[b_id]
            a_top = sorted(a_embs, key=lambda e: np.linalg.norm(e), reverse=True)[:5]
            b_top = sorted(b_embs, key=lambda e: np.linalg.norm(e), reverse=True)[:5]
            sim = max(float(np.dot(a, b)) for a in a_top for b in b_top)
            if sim >= min_sim:
                suggestions.append({'similarity': round(sim, 3), 'customer_a': {'id': a_c.id, 'customer_number': a_c.customer_number, 'name': a_c.name, 'visit_count': a_c.visit_count}, 'customer_b': {'id': b_c.id, 'customer_number': b_c.customer_number, 'name': b_c.name, 'visit_count': b_c.visit_count}})
    suggestions.sort(key=lambda x: x['similarity'], reverse=True)
    return jsonify(suggestions)


@bp.route('/api/customers/exclusions', methods=['POST'])
def api_customers_add_exclusion():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    data   = request.get_json() or {}
    id_a   = data.get('customer_a_id'); id_b = data.get('customer_b_id')
    reason = (data.get('reason') or 'Declined by user')[:200]
    if not id_a or not id_b or id_a == id_b: return jsonify({'error': 'Two distinct customer IDs required'}), 400
    lo, hi = min(id_a, id_b), max(id_a, id_b)
    existing = db.session.execute(db.text("SELECT id FROM customer_exclusions WHERE customer_id_a=:a AND customer_id_b=:b"), {'a': lo, 'b': hi}).fetchone()
    if not existing:
        db.session.execute(db.text("INSERT INTO customer_exclusions (customer_id_a, customer_id_b, reason) VALUES (:a, :b, :r)"), {'a': lo, 'b': hi, 'r': reason})
        db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/customers/merge_suggest_primary', methods=['POST'])
def api_merge_suggest_primary():
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}; ids = data.get('ids', [])
    if len(ids) < 2: return jsonify({'error': 'Need at least 2 ids'}), 400
    rows = []
    for cid in ids:
        row = db.session.execute(text('''SELECT c.id, c.name, c.visit_count,
               EXISTS(SELECT 1 FROM customer_faces WHERE customer_id=c.id AND active=TRUE),
               EXISTS(SELECT 1 FROM customer_gaits WHERE customer_id=c.id AND active=TRUE),
               c.first_seen FROM customers c WHERE c.id = :id'''), {'id': cid}).fetchone()
        if row: rows.append(row)
    if not rows: return jsonify({'error': 'No valid customers'}), 404
    scored = [(r, _merge_primary_score(r)) for r in rows]
    scored.sort(key=lambda x: x[1], reverse=True)
    primary_row, _ = scored[0]
    reasons = []
    if primary_row[1]: reasons.append('named')
    if primary_row[2]: reasons.append(f'{primary_row[2]} visits')
    if primary_row[3]: reasons.append('face enrolled')
    if primary_row[4]: reasons.append('gait enrolled')
    return jsonify({'primary_id': primary_row[0], 'reason': ', '.join(reasons) or 'best candidate', 'scores': [{'id': r[0], 'score': s} for r, s in scored]})


@bp.route('/api/customers/merge', methods=['POST'])
def api_customers_merge():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    data       = request.json or {}
    primary_id = data.get('primary_id')
    merge_ids  = data.get('merge_ids', [])
    auto_merged = data.get('auto_merged', False)
    similarity  = data.get('similarity')
    all_ids = ([primary_id] if primary_id else []) + list(merge_ids)
    if len(all_ids) < 2: return jsonify({'error': 'Need at least 2 customer ids'}), 400
    if not primary_id:
        rows = []
        for cid in all_ids:
            row = db.session.execute(text('''SELECT c.id, c.name, c.visit_count,
                   EXISTS(SELECT 1 FROM customer_faces WHERE customer_id=c.id AND active=TRUE),
                   EXISTS(SELECT 1 FROM customer_gaits WHERE customer_id=c.id AND active=TRUE),
                   c.first_seen FROM customers c WHERE c.id = :id'''), {'id': cid}).fetchone()
            if row: rows.append(row)
        rows.sort(key=_merge_primary_score, reverse=True)
        primary_id = rows[0][0]; merge_ids = [r[0] for r in rows[1:]]
    elif not merge_ids:
        return jsonify({'error': 'merge_ids required when primary_id provided'}), 400
    try:
        if not db.session.execute(text('SELECT id FROM customers WHERE id = :id'), {'id': primary_id}).fetchone():
            return jsonify({'error': 'Primary customer not found'}), 404
        merged_count = 0
        for mid in merge_ids:
            if mid == primary_id: continue
            src = db.session.execute(text('SELECT id, visit_count, last_visit, first_seen, name, phone, email, notes, is_employee FROM customers WHERE id = :id'), {'id': mid}).fetchone()
            if not src: continue
            src_photo = db.session.execute(text('SELECT photo FROM customer_faces WHERE customer_id = :sid AND active = TRUE AND photo IS NOT NULL ORDER BY LENGTH(photo) DESC LIMIT 1'), {'sid': mid}).fetchone()
            src_face_photo = src_photo[0] if src_photo else None
            for tbl in ['customer_faces', 'customer_gaits', 'customer_physical_attributes']:
                db.session.execute(text(f'UPDATE {tbl} SET original_customer_id = :sid WHERE customer_id = :sid AND original_customer_id IS NULL'), {'sid': mid})
            db.session.execute(text('UPDATE customer_faces SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(text('UPDATE customer_gaits SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(text('UPDATE customer_visits SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(text('UPDATE customer_physical_attributes SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(text('UPDATE visit_sessions SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(text('UPDATE sales SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(text('UPDATE customer_plates SET customer_id = :pid WHERE customer_id = :sid AND plate_number NOT IN (SELECT plate_number FROM customer_plates WHERE customer_id = :pid)'), {'pid': primary_id, 'sid': mid})
            db.session.execute(text('DELETE FROM customer_plates WHERE customer_id = :sid'), {'sid': mid})
            db.session.execute(text('UPDATE customers SET visit_count = visit_count + :vc, last_visit = GREATEST(last_visit, :lv), first_seen = LEAST(first_seen, :fs) WHERE id = :pid'), {'pid': primary_id, 'vc': src[1] or 0, 'lv': src[2], 'fs': src[3]})
            pri = db.session.execute(text('SELECT name, phone, email, notes, is_employee FROM customers WHERE id = :pid'), {'pid': primary_id}).fetchone()
            updates = {}
            if src[4] and not pri[0]: updates['name']  = src[4]
            if src[5] and not pri[1]: updates['phone'] = src[5]
            if src[6] and not pri[2]: updates['email'] = src[6]
            if src[7] and not pri[3]: updates['notes'] = src[7]
            if src[8] and not pri[4]: updates['is_employee'] = True
            if updates:
                set_clause = ', '.join(f'{k} = :{k}' for k in updates)
                updates['pid'] = primary_id
                db.session.execute(text(f'UPDATE customers SET {set_clause} WHERE id = :pid'), updates)
            db.session.execute(text('UPDATE customers SET active = FALSE, merged_into = :pid WHERE id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(text('''INSERT INTO customer_merge_log (primary_id, source_id, merged_at, auto_merged, similarity, source_name, source_customer_number, source_visit_count, source_face_photo) VALUES (:pid, :sid, NOW(), :auto, :sim, :name, :cnum, :vc, :photo)'''),
                {'pid': primary_id, 'sid': mid, 'auto': auto_merged, 'sim': float(similarity) if similarity is not None else None, 'name': src[4], 'cnum': db.session.execute(text('SELECT customer_number FROM customers WHERE id=:id'), {'id': mid}).scalar(), 'vc': src[1], 'photo': src_face_photo})
            merged_count += 1

        import numpy as np
        face_rows = db.session.execute(text('SELECT id, embedding, photo FROM customer_faces WHERE customer_id = :pid AND active = TRUE'), {'pid': primary_id}).fetchall()
        if face_rows:
            MAX_EMBEDDINGS = int(float(get_setting('max_face_angles', 24) or 24))
            MIN_DISTANCE   = float(get_setting('min_angle_distance', 0.25) or 0.25)
            normed = []
            skipped_dims = set()
            for row in face_rows:
                emb = np.frombuffer(row[1], dtype=np.float32)
                if emb.shape == (512,):
                    n = np.linalg.norm(emb)
                    if n > 0: normed.append((emb / n, bytes(emb.tobytes()), row[2]))
                else:
                    skipped_dims.add(emb.shape)
            if skipped_dims:
                import logging
                logging.getLogger(__name__).warning(
                    'customer merge pid=%s: dropped %d face embedding(s) with unexpected dimensions %s',
                    primary_id, len(skipped_dims), skipped_dims
                )
            selected = []
            for normed_emb, raw_bytes, photo in normed:
                if all(float(np.dot(normed_emb, s[0])) < (1.0 - MIN_DISTANCE) for s in selected):
                    selected.append((normed_emb, raw_bytes, photo))
                if len(selected) >= MAX_EMBEDDINGS: break
            db.session.execute(text('UPDATE customer_faces SET active = FALSE WHERE customer_id = :pid'), {'pid': primary_id})
            for _, raw_bytes, photo in selected:
                db.session.execute(text('INSERT INTO customer_faces (customer_id, embedding, photo, enrolled_at, active) VALUES (:pid, :emb, :photo, NOW(), TRUE)'), {'pid': primary_id, 'emb': raw_bytes, 'photo': photo})

        db.session.commit()
        return jsonify({'ok': True, 'merged': merged_count, 'primary_id': primary_id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/customers/merge_log/<int:log_id>/unmerge', methods=['POST'])
def api_customers_unmerge(log_id):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    try:
        log = db.session.execute(text('SELECT id, primary_id, source_id, unmerged_at FROM customer_merge_log WHERE id = :id'), {'id': log_id}).fetchone()
        if not log: return jsonify({'error': 'Merge log entry not found'}), 404
        if log[3] is not None: return jsonify({'error': 'Already unmerged'}), 400
        primary_id = log[1]; source_id = log[2]
        src = db.session.execute(text('SELECT id, merged_into FROM customers WHERE id = :id'), {'id': source_id}).fetchone()
        if not src: return jsonify({'error': 'Source customer not found'}), 404
        moved_faces = db.session.execute(text('UPDATE customer_faces SET customer_id = :sid WHERE customer_id = :pid AND original_customer_id = :sid'), {'pid': primary_id, 'sid': source_id}).rowcount
        db.session.execute(text('UPDATE customer_gaits SET customer_id = :sid WHERE customer_id = :pid AND original_customer_id = :sid'), {'pid': primary_id, 'sid': source_id})
        db.session.execute(text('UPDATE customer_physical_attributes SET customer_id = :sid WHERE customer_id = :pid AND original_customer_id = :sid'), {'pid': primary_id, 'sid': source_id})
        db.session.execute(text('UPDATE customers SET active = TRUE, merged_into = NULL WHERE id = :sid'), {'sid': source_id})
        _recompute_customer_embeddings(source_id,  text)
        _recompute_customer_embeddings(primary_id, text)
        db.session.execute(text('UPDATE customer_merge_log SET unmerged_at = NOW() WHERE id = :id'), {'id': log_id})
        soft = moved_faces == 0
        db.session.commit()
        return jsonify({'ok': True, 'soft_unmerge': soft, 'message': 'Customer reactivated. Biometric data will rebuild automatically.' if soft else 'Customer reactivated with their original biometric data.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/customers/<int:cid>/merge_history', methods=['GET'])
def api_customer_merge_history(cid):
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    import base64 as _b64
    absorbed = db.session.execute(text('''SELECT ml.id, ml.source_id, ml.merged_at, ml.auto_merged, ml.similarity, ml.source_name, ml.source_customer_number, ml.source_visit_count, ml.source_face_photo, ml.unmerged_at, c.active FROM customer_merge_log ml LEFT JOIN customers c ON c.id = ml.source_id WHERE ml.primary_id = :cid ORDER BY ml.merged_at DESC'''), {'cid': cid}).fetchall()
    is_source = db.session.execute(text('''SELECT ml.id, ml.primary_id, ml.merged_at, ml.auto_merged, ml.similarity, ml.unmerged_at, c.name, c.customer_number FROM customer_merge_log ml JOIN customers c ON c.id = ml.primary_id WHERE ml.source_id = :cid ORDER BY ml.merged_at DESC LIMIT 1'''), {'cid': cid}).fetchone()
    def fmt_photo(b):
        if not b: return None
        try: return 'data:image/jpeg;base64,' + _b64.b64encode(bytes(b)).decode()
        except Exception: return None
    return jsonify({'absorbed': [{'log_id': r[0], 'source_id': r[1], 'merged_at': r[2].isoformat() if r[2] else None, 'auto_merged': r[3], 'similarity': float(r[4]) if r[4] is not None else None, 'source_name': r[5], 'source_customer_number': r[6], 'source_visit_count': r[7], 'source_face_photo': fmt_photo(r[8]), 'unmerged_at': r[9].isoformat() if r[9] else None, 'source_active': r[10]} for r in absorbed], 'merged_into': {'log_id': is_source[0], 'primary_id': is_source[1], 'merged_at': is_source[2].isoformat() if is_source[2] else None, 'unmerged_at': is_source[5].isoformat() if is_source[5] else None, 'primary_name': is_source[6], 'primary_number': is_source[7]} if is_source and not is_source[5] else None})


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

@bp.route('/api/customers/<int:cid>/enroll/plate', methods=['POST'])
def api_customers_enroll_plate(cid):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c: return jsonify({'error': 'Not found'}), 404
    plate = (request.json or {}).get('plate_number', '').strip().upper()
    if not plate: return jsonify({'error': 'plate_number required'}), 400
    if CustomerPlate.query.filter_by(plate_number=plate).first(): return jsonify({'error': 'Plate already enrolled'}), 409
    db.session.add(CustomerPlate(customer_id=cid, plate_number=plate))
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/customers/<int:cid>/enroll/plate/<int:pid>', methods=['DELETE'])
def api_customers_delete_plate(cid, pid):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    cp = db.session.get(CustomerPlate, pid)
    if not cp or cp.customer_id != cid: return jsonify({'error': 'Not found'}), 404
    db.session.delete(cp); db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/customers/<int:cid>/enroll/face', methods=['POST'])
def api_customers_enroll_face(cid):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c: return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    embedding_b64 = data.get('embedding_b64')
    if not embedding_b64: return jsonify({'error': 'embedding_b64 required'}), 400
    embedding_bytes  = base64.b64decode(embedding_b64)
    photo_bytes      = base64.b64decode(data['photo_b64']) if data.get('photo_b64') else None
    body_photo_bytes = base64.b64decode(data['body_photo_b64']) if data.get('body_photo_b64') else None
    snapshot_only    = data.get('snapshot_only', False)
    camera_source_val = data.get('camera_source') or None

    if snapshot_only:
        existing_body_row = CustomerFace.query.filter_by(customer_id=cid).filter(CustomerFace.body_photo != None).order_by(CustomerFace.enrolled_at.desc()).first()
        if body_photo_bytes:
            if not existing_body_row:
                db.session.add(CustomerFace(customer_id=cid, embedding=embedding_bytes, photo=photo_bytes, body_photo=body_photo_bytes, active=False))
            else:
                existing_body_row.body_photo = body_photo_bytes
                if photo_bytes: existing_body_row.photo = photo_bytes
        db.session.commit()
        is_new_angle = None
    else:
        import numpy as np
        MAX_EMBEDDINGS    = int(float(get_setting('max_face_angles', 24) or 24))
        MIN_DISTANCE      = float(get_setting('min_angle_distance', 0.25) or 0.25)
        replace_if_better = data.get('replace_if_better', False)
        new_quality       = float(data.get('quality', 0.0))
        new_emb = np.frombuffer(embedding_bytes, dtype=np.float32).copy()
        norm = np.linalg.norm(new_emb)
        if norm > 0: new_emb /= norm
        existing    = CustomerFace.query.filter_by(customer_id=cid, active=True).all()
        is_new_angle = True; replaced = False
        for row in existing:
            stored = np.frombuffer(row.embedding, dtype=np.float32).copy()
            s_norm = np.linalg.norm(stored)
            if s_norm > 0: stored /= s_norm
            sim = float(np.dot(new_emb, stored))
            if sim > (1.0 - MIN_DISTANCE):
                stored_quality = float(row.quality) if row.quality else 0.0
                if replace_if_better and new_quality > 0 and new_quality > stored_quality + 0.10:
                    row.active = False; db.session.flush()
                    db.session.add(CustomerFace(customer_id=cid, embedding=embedding_bytes, photo=photo_bytes or row.photo, body_photo=body_photo_bytes, quality=new_quality, camera_source=camera_source_val))
                    replaced = True; is_new_angle = False
                else:
                    if photo_bytes and (not row.photo or len(photo_bytes) > len(row.photo)): row.photo = photo_bytes
                    is_new_angle = False
                break
        if is_new_angle and not replaced:
            if len(existing) >= MAX_EMBEDDINGS:
                min(existing, key=lambda r: r.enrolled_at).active = False
            db.session.add(CustomerFace(customer_id=cid, embedding=embedding_bytes, photo=photo_bytes, body_photo=body_photo_bytes, quality=new_quality if new_quality > 0 else None, camera_source=camera_source_val))
        db.session.commit()
    return jsonify({'ok': True, 'new_angle': is_new_angle})


@bp.route('/api/customers/<int:cid>/enroll/gait', methods=['POST'])
def api_customers_enroll_gait(cid):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c: return jsonify({'error': 'Not found'}), 404
    features_b64 = (request.json or {}).get('features_b64')
    if not features_b64: return jsonify({'error': 'features_b64 required'}), 400
    features_bytes = base64.b64decode(features_b64)
    CustomerGait.query.filter_by(customer_id=cid).update({'active': False})
    db.session.add(CustomerGait(customer_id=cid, gait_features=features_bytes))
    db.session.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Photo endpoints
# ---------------------------------------------------------------------------

@bp.route('/api/customers/<int:cid>/photo', methods=['GET'])
def api_customer_photo(cid):
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    for row in CustomerFace.query.filter_by(customer_id=cid, active=True).filter(CustomerFace.photo != None).filter(CustomerFace.quality != None).order_by(CustomerFace.quality.desc()).all():
        if row.photo and len(row.photo) >= 4000: return Response(row.photo, mimetype='image/jpeg')
    for row in CustomerFace.query.filter_by(customer_id=cid, active=True).filter(CustomerFace.photo != None).filter(CustomerFace.quality != None).all():
        if row.photo: return Response(row.photo, mimetype='image/jpeg')
    row = CustomerFace.query.filter_by(customer_id=cid, active=True).filter(CustomerFace.photo != None).filter(CustomerFace.quality == None).order_by(CustomerFace.enrolled_at.desc()).first()
    if row and row.photo: return Response(row.photo, mimetype='image/jpeg')
    snap = CustomerFace.query.filter_by(customer_id=cid).filter(CustomerFace.body_photo != None).order_by(CustomerFace.enrolled_at.desc()).first()
    if snap and snap.body_photo: return Response(snap.body_photo, mimetype='image/jpeg')
    return '', 404


@bp.route('/api/customers/<int:cid>/body_photo', methods=['GET'])
def api_customer_body_photo(cid):
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    row = CustomerFace.query.filter_by(customer_id=cid).filter(CustomerFace.body_photo != None).order_by(CustomerFace.enrolled_at.desc()).first()
    if not row or not row.body_photo: return '', 404
    return Response(row.body_photo, mimetype='image/jpeg')


# ---------------------------------------------------------------------------
# Recognition service integration
# ---------------------------------------------------------------------------

@bp.route('/api/customers/identify', methods=['POST'])
def api_customers_identify():
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    cid = data.get('customer_id')
    if not cid: return jsonify({'error': 'customer_id required'}), 400
    c = db.session.get(Customer, cid)
    if not c: return jsonify({'error': 'Not found'}), 404
    if not c.active and c.merged_into:
        primary = db.session.get(Customer, c.merged_into)
        if primary and primary.active: c = primary; cid = c.id
    camera_source = data.get('camera_source')
    visit_min_gap = int(float(get_setting('visit_min_gap_seconds', 180) or 180))
    if camera_source:
        recent = db.session.execute(text('SELECT detected_at FROM customer_visits WHERE customer_id = :cid AND camera_source = :cam ORDER BY detected_at DESC LIMIT 1'), {'cid': cid, 'cam': camera_source}).fetchone()
        if recent and recent[0]:
            gap = (datetime.utcnow() - recent[0]).total_seconds()
            if gap < visit_min_gap: return jsonify({'ok': True, 'skipped': True, 'reason': 'too_soon', 'gap_seconds': int(gap)})
    visit = CustomerVisit(customer_id=cid, matched_signals=data.get('matched_signals', ''), confidence_scores=_json.dumps(data.get('confidence_scores')) if data.get('confidence_scores') else None, camera_source=camera_source)
    db.session.add(visit)
    c.visit_count = (c.visit_count or 0) + 1
    c.last_visit  = datetime.utcnow()
    if not c.is_pos_customer: c.is_pos_customer = True
    dwell_seconds = data.get('dwell_seconds')
    if dwell_seconds and int(dwell_seconds) > 0:
        now_utc = datetime.utcnow()
        db.session.execute(text("INSERT INTO visit_sessions (customer_id, session_start, session_end, entry_camera, dwell_seconds) VALUES (:cid, :start, :end, :cam, :dwell)"), {'cid': cid, 'start': now_utc - timedelta(seconds=int(dwell_seconds)), 'end': now_utc, 'cam': camera_source, 'dwell': int(dwell_seconds)})
    db.session.commit()
    return jsonify({'ok': True, 'visit_id': visit.id})


@bp.route('/api/customers/log_plate', methods=['POST'])
def api_customers_log_plate():
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    db.session.add(PlateDetection(plate_number=data.get('plate_number', '').upper(), confidence=data.get('confidence'), customer_id=data.get('customer_id'), matched=bool(data.get('matched', False)), snapshot_path=data.get('snapshot_path'), camera_source=data.get('camera_source')))
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/customers/pending_visits', methods=['GET'])
def api_customers_pending_visits():
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    cutoff = datetime.utcnow() - timedelta(minutes=5)
    visits = CustomerVisit.query.filter_by(acknowledged=False).filter(CustomerVisit.detected_at >= cutoff).order_by(CustomerVisit.detected_at.desc()).all()
    result = []; seen = set()
    for v in visits:
        c = db.session.get(Customer, v.customer_id)
        if not c: continue
        if not c.active and c.merged_into:
            primary = db.session.get(Customer, c.merged_into)
            if primary and primary.active: c = primary
        if not c.active or not (c.name or c.customer_number) or c.id in seen: continue
        seen.add(c.id)
        result.append({'id': v.id, 'customer_name': c.name or c.customer_number, 'visit_count': c.visit_count, 'matched_signals': v.matched_signals, 'detected_at': v.detected_at.isoformat()})
    return jsonify(result)


@bp.route('/api/customers/visits/<int:vid>/acknowledge', methods=['POST'])
def api_customers_acknowledge_visit(vid):
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    v = db.session.get(CustomerVisit, vid)
    if v: v.acknowledged = True; db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/customers/faces_raw', methods=['GET'])
def api_customers_faces_raw():
    # Biometric data — admin or recognition service only (POPIA compliance)
    if not require_role('admin', 'developer'): return jsonify({'error': 'Forbidden'}), 403
    rows = CustomerFace.query.filter_by(active=True).all()
    return jsonify([{'customer_id': r.customer_id, 'embedding_b64': base64.b64encode(r.embedding).decode()} for r in rows])


@bp.route('/api/customers/gaits_raw', methods=['GET'])
def api_customers_gaits_raw():
    if not require_role('admin', 'developer'): return jsonify({'error': 'Forbidden'}), 403
    rows = CustomerGait.query.filter_by(active=True).all()
    return jsonify([{'customer_id': r.customer_id, 'features_b64': base64.b64encode(r.gait_features).decode()} for r in rows])


@bp.route('/api/customers/<int:cid>/faces_raw', methods=['GET'])
def api_customer_faces_raw(cid):
    if not require_role('admin', 'developer'): return jsonify({'error': 'Forbidden'}), 403
    rows = CustomerFace.query.filter_by(customer_id=cid, active=True).order_by(CustomerFace.enrolled_at.desc()).limit(10).all()
    return jsonify([{'embedding_b64': base64.b64encode(r.embedding).decode(), 'camera': r.camera_source, 'quality': float(r.quality) if r.quality is not None else None} for r in rows])


@bp.route('/api/customers/<int:cid>/gaits_raw', methods=['GET'])
def api_customer_gaits_raw(cid):
    if not require_role('admin', 'developer'): return jsonify({'error': 'Forbidden'}), 403
    rows = CustomerGait.query.filter_by(customer_id=cid, active=True).all()
    return jsonify([{'features_b64': base64.b64encode(r.gait_features).decode()} for r in rows])


@bp.route('/api/customers/plate_log', methods=['GET'])
def api_customers_plate_log():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    limit = int(request.args.get('limit', 50))
    rows  = PlateDetection.query.order_by(PlateDetection.detected_at.desc()).limit(limit).all()
    return jsonify([{'id': r.id, 'plate_number': r.plate_number, 'confidence': float(r.confidence) if r.confidence else None, 'detected_at': r.detected_at.isoformat(), 'customer_id': r.customer_id, 'matched': r.matched, 'camera_source': r.camera_source} for r in rows])


@bp.route('/api/customers/max_number', methods=['GET'])
def api_customers_max_number():
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    max_c = Customer.query.filter(Customer.customer_number.isnot(None)).order_by(Customer.customer_number.desc()).first()
    if max_c and max_c.customer_number:
        try:
            cn = max_c.customer_number
            return jsonify({'max_number': int(cn.split('-')[1]) if '-' in cn else int(cn)})
        except (IndexError, ValueError): pass
    return jsonify({'max_number': 0})


@bp.route('/api/customers/<int:cid>/attributes', methods=['GET', 'POST'])
def api_customer_attributes(cid):
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    if not db.session.get(Customer, cid): return jsonify({'error': 'Customer not found'}), 404
    if request.method == 'GET':
        return jsonify(_voted_attributes(_fetch_attr_rows(cid)))
    data = request.get_json()
    db.session.execute(text("""INSERT INTO customer_physical_attributes (customer_id, height_cm, hair_color, skin_tone, build, eye_color, age_range, gender, wearing_glasses, facial_hair, camera_source, confidence, height_category) VALUES (:cid, :height, :hair, :skin, :build, :eye, :age, :gender, :glasses, :facial, :camera, :conf, :height_cat)"""),
        {'cid': cid, 'height': data.get('height_cm'), 'hair': data.get('hair_color'), 'skin': data.get('skin_tone'), 'build': data.get('build'), 'eye': data.get('eye_color'), 'age': data.get('age_range'), 'gender': data.get('gender'), 'glasses': data.get('wearing_glasses'), 'facial': data.get('facial_hair'), 'camera': data.get('camera_source'), 'conf': data.get('confidence'), 'height_cat': data.get('height_category')})
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/customers/attributes_bulk', methods=['GET'])
def api_customers_attributes_bulk():
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    result = db.session.execute(text(f"""SELECT customer_id, height_cm, hair_color, skin_tone, build, eye_color, age_range, gender, wearing_glasses, facial_hair, detected_at, camera_source, confidence, height_category FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY detected_at DESC) AS rn FROM customer_physical_attributes) ranked WHERE rn <= {_ATTR_WINDOW} ORDER BY customer_id, detected_at DESC""")).fetchall()
    rows_by_cid = defaultdict(list)
    for row in result: rows_by_cid[row[0]].append(row[1:])
    return jsonify({str(cid): voted for cid, rows in rows_by_cid.items() if (voted := _voted_attributes(rows))})


# ---------------------------------------------------------------------------
# Till
# ---------------------------------------------------------------------------

@bp.route('/api/till/active_customer', methods=['GET'])
def api_till_active_customer():
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=30)
        result = db.session.execute(text("""SELECT td.customer_id, td.detected_at, c.name, c.customer_number FROM till_detections td JOIN customers c ON c.id = td.customer_id WHERE td.detected_at >= :cutoff AND c.name IS NOT NULL ORDER BY td.detected_at DESC LIMIT 1"""), {'cutoff': cutoff}).fetchone()
    except Exception:
        db.session.rollback()
        return jsonify({'customer_id': None})
    if not result: return jsonify({'customer_id': None})
    return jsonify({'customer_id': result[0], 'name': result[2], 'customer_number': result[3], 'detected_at': result[1].isoformat() if result[1] else None})


@bp.route('/api/till/detect', methods=['POST'])
def api_till_detect():
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    db.session.execute(text("INSERT INTO till_detections (customer_id, camera_source) VALUES (:cid, :camera)"), {'cid': data['customer_id'], 'camera': data.get('camera_source')})
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/customers/visits/recent', methods=['GET'])
def api_customers_visits_recent():
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    cutoff = datetime.utcnow() - timedelta(hours=int(request.args.get('hours', 2)))
    result = db.session.execute(text("SELECT cv.customer_id, cv.detected_at, cv.camera_source FROM customer_visits cv WHERE cv.detected_at >= :cutoff ORDER BY cv.customer_id, cv.detected_at"), {'cutoff': cutoff}).fetchall()
    return jsonify([{'customer_id': r[0], 'detected_at': r[1].isoformat() if r[1] else None, 'camera_source': r[2]} for r in result])


@bp.route('/api/customers/sessions', methods=['POST'])
def api_customers_sessions():
    if not require_login(): return jsonify({'error': 'Unauthorized'}), 401
    data     = request.get_json()
    existing = db.session.execute(text("SELECT id FROM visit_sessions WHERE customer_id = :cid AND session_start = :start LIMIT 1"), {'cid': data['customer_id'], 'start': datetime.fromisoformat(data['session_start'])}).fetchone()
    if existing: return jsonify({'ok': True, 'id': existing[0], 'already_exists': True})
    db.session.execute(text("INSERT INTO visit_sessions (customer_id, session_start, session_end, entry_camera, checkout_camera, dwell_seconds, purchase_made, sale_ids) VALUES (:cid, :start, :end, :entry, :checkout, :dwell, :purchase, :sales)"),
        {'cid': data['customer_id'], 'start': datetime.fromisoformat(data['session_start']), 'end': datetime.fromisoformat(data['session_end']), 'entry': data.get('entry_camera'), 'checkout': data.get('checkout_camera'), 'dwell': data.get('dwell_seconds', 0), 'purchase': data.get('purchase_made', False), 'sales': data.get('sale_ids')})
    db.session.commit()
    return jsonify({'ok': True})
