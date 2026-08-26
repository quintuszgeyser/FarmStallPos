"""
Employee management, time & attendance, and payroll.

GET    /api/employees                              admin
POST   /api/employees                              admin
PUT    /api/employees/<id>                         admin
DELETE /api/employees/<id>                         admin (soft deactivate)

GET    /api/employees/<id>                         admin | own teller
GET    /api/employees/<id>/deductions              admin | own teller
POST   /api/employees/<id>/deductions              admin
PUT    /api/employees/<id>/deductions/<did>        admin
DELETE /api/employees/<id>/deductions/<did>        admin

GET    /api/employees/<id>/attendance?month=YYYY-MM  admin | own teller
POST   /api/employees/<id>/attendance              admin (upsert one row)
DELETE /api/employees/<id>/attendance/<aid>        admin

GET    /api/employees/<id>/schedule?month=YYYY-MM  admin | own teller
POST   /api/employees/<id>/schedule                admin (upsert one row)
DELETE /api/employees/<id>/schedule/<sid>          admin

GET    /api/employees/<id>/leaves                  admin | own teller
POST   /api/employees/<id>/leaves                  admin | own teller (request)
PUT    /api/employees/<id>/leaves/<lid>/approve    admin
PUT    /api/employees/<id>/leaves/<lid>/reject     admin

GET    /api/employees/<id>/advances                admin | own teller
POST   /api/employees/<id>/advances                admin
PUT    /api/employees/<id>/advances/<aid>/cancel   admin

GET    /api/employees/<id>/loans                   admin | own teller
POST   /api/employees/<id>/loans                   admin

GET    /api/employees/<id>/documents               admin
POST   /api/employees/<id>/documents               admin
DELETE /api/employees/<id>/documents/<did>         admin
GET    /api/employees/<id>/documents/<did>/download admin

GET    /api/employees/<id>/pay_runs                admin | own teller
POST   /api/employees/<id>/pay_runs/preview        admin
POST   /api/employees/<id>/pay_runs                admin (save as draft)
PUT    /api/employees/<id>/pay_runs/<pid>/approve  admin
PUT    /api/employees/<id>/pay_runs/<pid>/paid     admin
DELETE /api/employees/<id>/pay_runs/<pid>          admin (draft only)
GET    /api/employees/<id>/pay_runs/<pid>/payslip  admin | own teller (HTML)

POST   /api/employees/pay_runs/bulk                admin (all employees, one period)

GET    /api/employees/schedule_rules               admin
POST   /api/employees/schedule_rules               admin

GET    /api/employees/pay_rules                    admin
PUT    /api/employees/pay_rules/<id>               admin

GET    /api/employees/public_holidays?year=        all roles
GET    /api/employees/dashboard                    admin
"""
import os
import json
import math
from datetime import datetime, date, timedelta
from decimal import Decimal

from flask import Blueprint, jsonify, request, render_template, session
from sqlalchemy import func

from helpers import require_login, require_role, current_user, get_setting
from models import (
    db, User, Setting,
    Employee, EmployeeDeduction, PayRule, EmployeeAttendance,
    ShiftSchedule, LeaveRequest, LeaveBalance, LeavePolicy,
    EmployeeLeaveAdjustment,
    EmployeeAdvance, EmployeeLoan, EmployeeDocument, PayRun,
)

bp = Blueprint('employees', __name__)

EMPLOYEE_DOCS_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'employee_docs')

# ── SA Public Holidays ────────────────────────────────────────────────────────

_FIXED_HOLIDAYS = [
    (1,  1,  "New Year's Day"),
    (3,  21, "Human Rights Day"),
    (4,  27, "Freedom Day"),
    (5,  1,  "Workers' Day"),
    (6,  16, "Youth Day"),
    (8,  9,  "National Women's Day"),
    (9,  24, "Heritage Day"),
    (12, 16, "Day of Reconciliation"),
    (12, 25, "Christmas Day"),
    (12, 26, "Day of Goodwill"),
]

def _easter(year):
    """Computus — returns Easter Sunday date."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day   = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)

def sa_public_holidays(year):
    """Return {date: name} dict for SA public holidays in the given year.
    If a fixed holiday falls on Sunday, the following Monday is also a holiday."""
    holidays = {}
    easter = _easter(year)
    holidays[easter - timedelta(days=2)] = "Good Friday"
    holidays[easter + timedelta(days=1)] = "Family Day"
    for month, day, name in _FIXED_HOLIDAYS:
        d = date(year, month, day)
        holidays[d] = name
        if d.weekday() == 6:  # Sunday → Monday also public holiday
            holidays[d + timedelta(days=1)] = f"{name} (observed)"
    return holidays


# ── SA PAYE calculation (2025/2026 tax year) ─────────────────────────────────

def _paye_annual(annual_gross):
    """Return estimated annual PAYE for a given annual gross (Decimal). Uses 2025/2026 tables."""
    g = Decimal(str(annual_gross))
    PRIMARY_REBATE  = Decimal('17235')
    THRESHOLD       = Decimal('95750')
    if g <= THRESHOLD:
        return Decimal('0')
    # Tax tables
    brackets = [
        (Decimal('237100'),  Decimal('0'),      Decimal('0.18')),
        (Decimal('370500'),  Decimal('42678'),   Decimal('0.26')),
        (Decimal('512800'),  Decimal('77362'),   Decimal('0.31')),
        (Decimal('673000'),  Decimal('121475'),  Decimal('0.36')),
        (Decimal('857900'),  Decimal('179147'),  Decimal('0.39')),
        (Decimal('1817000'), Decimal('251258'),  Decimal('0.41')),
        (None,               Decimal('644489'),  Decimal('0.45')),
    ]
    prev = Decimal('0')
    tax  = Decimal('0')
    for upper, base, rate in brackets:
        if upper is None or g <= upper:
            tax = base + (g - prev) * rate
            break
        prev = upper
    return max(Decimal('0'), tax - PRIMARY_REBATE)

def _uif_employee(gross, period_days=14):
    """Employee UIF contribution: 1% of gross, prorated monthly cap R177.12."""
    monthly_cap = Decimal('177.12')
    cap = monthly_cap * Decimal(str(period_days)) / Decimal('30')
    return min(Decimal(str(gross)) * Decimal('0.01'), cap).quantize(Decimal('0.01'))

def _uif_employer(gross, period_days=14):
    return _uif_employee(gross, period_days)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _own_or_admin(employee_id):
    """Return (employee, is_own, error_response). Tellers may only access their own record."""
    u = current_user()
    if not u:
        return None, False, (jsonify({'error': 'Login required'}), 401)
    emp = Employee.query.get(employee_id)
    if not emp:
        return None, False, (jsonify({'error': 'Employee not found'}), 404)
    is_admin = u.has_role('admin')
    is_own   = emp.user_id == u.id
    if not is_admin and not is_own:
        return None, False, (jsonify({'error': 'Forbidden'}), 403)
    return emp, is_own, None

def _serialize_employee(emp, include_sensitive=True):
    row = {
        'id':                   emp.id,
        'user_id':              emp.user_id,
        'name':                 emp.name,
        'employee_number':      emp.employee_number,
        'phone':                emp.phone,
        'start_date':           emp.start_date.isoformat() if emp.start_date else None,
        'employment_type':      emp.employment_type,
        'hourly_rate':          float(emp.hourly_rate),
        'normal_hours_per_day': float(emp.normal_hours_per_day),
        'normal_days_per_week': emp.normal_days_per_week,
        'pay_frequency':        emp.pay_frequency,
        'pay_day_of_week':      emp.pay_day_of_week,
        'leave_days_per_year':  float(emp.leave_days_per_year),
        'is_active':            emp.is_active,
        'notes':                emp.notes,
        'pay_type':             emp.pay_type or 'hourly',
        'work_days_json':       emp.work_days_json or '0,1,2,3,4,5',
        'rotation_start_day':   emp.rotation_start_day,
        'rotation_slot':        emp.rotation_slot,
        'username':             emp.user.username if emp.user else None,
    }
    if include_sensitive:
        row.update({
            'id_number':        emp.id_number,
            'tax_number':       emp.tax_number,
            'uif_number':       emp.uif_number,
            'bank_name':        emp.bank_name,
            'bank_account':     emp.bank_account,
            'bank_branch_code': emp.bank_branch_code,
        })
    return row

def _serialize_attendance(a):
    return {
        'id':             a.id,
        'employee_id':    a.employee_id,
        'work_date':      a.work_date.isoformat(),
        'clock_in':       a.clock_in.strftime('%H:%M') if a.clock_in else None,
        'clock_out':      a.clock_out.strftime('%H:%M') if a.clock_out else None,
        'break_minutes':  a.break_minutes,
        'hours_worked':   float(a.hours_worked) if a.hours_worked is not None else None,
        'day_type':       a.day_type,
        'source':         a.source,
        'notes':          a.notes,
        'approved_by':    a.approved_by,
        'created_at':     a.created_at.isoformat(),
    }

def _serialize_pay_run(pr, brief=False):
    row = {
        'id':           pr.id,
        'reference':    pr.reference,
        'employee_id':  pr.employee_id,
        'period_start': pr.period_start.isoformat(),
        'period_end':   pr.period_end.isoformat(),
        'pay_date':     pr.pay_date.isoformat() if pr.pay_date else None,
        'gross_pay':    float(pr.gross_pay),
        'net_pay':      float(pr.net_pay),
        'status':       pr.status,
        'created_at':   pr.created_at.isoformat(),
        'approved_at':  pr.approved_at.isoformat() if pr.approved_at else None,
        'paid_at':      pr.paid_at.isoformat() if pr.paid_at else None,
    }
    if not brief:
        row.update({
            'hourly_rate_snapshot':       float(pr.hourly_rate_snapshot),
            'normal_hours':               float(pr.normal_hours),
            'overtime_hours':             float(pr.overtime_hours),
            'sunday_hours':               float(pr.sunday_hours),
            'holiday_hours':              float(pr.holiday_hours),
            'vacation_hours':             float(pr.vacation_hours),
            'sick_hours':                 float(pr.sick_hours),
            'normal_pay':                 float(pr.normal_pay),
            'overtime_pay':               float(pr.overtime_pay),
            'sunday_pay':                 float(pr.sunday_pay),
            'holiday_pay':                float(pr.holiday_pay),
            'vacation_pay':               float(pr.vacation_pay),
            'total_deductions':           float(pr.total_deductions),
            'deductions':                 json.loads(pr.deductions_json),
            'employer_contributions':     json.loads(pr.employer_contributions_json),
            'advances':                   json.loads(pr.advances_json),
            'attendance':                 json.loads(pr.attendance_json),
            'notes':                      pr.notes,
        })
    return row

def _compute_hours(clock_in_str, clock_out_str, break_minutes):
    """Compute hours_worked from clock in/out strings (HH:MM) minus break.
    Defaults to 60 min lunch deduction when no break is specified."""
    def parse_t(s):
        h, m = map(int, s.split(':'))
        return h * 60 + m
    try:
        raw_minutes = parse_t(clock_out_str) - parse_t(clock_in_str)
        if break_minutes is not None and int(break_minutes) > 0:
            effective_break = int(break_minutes)
        elif raw_minutes > 300:  # only auto-deduct lunch for shifts longer than 5 hours
            effective_break = 60
        else:
            effective_break = 0
        total = raw_minutes - effective_break
        return round(max(0, total) / 60, 2)
    except Exception:
        return None

def _auto_day_type(work_date, holidays):
    """Return the appropriate day_type for a given date."""
    if work_date in holidays:
        return 'public_holiday'
    if work_date.weekday() == 6:  # Sunday
        return 'sunday'
    return 'normal'

def _get_or_init_pay_rule(day_type):
    rule = PayRule.query.filter_by(day_type=day_type).first()
    return rule.multiplier if rule else Decimal('1')

def _calculate_pay_run(employee_id, period_start, period_end):
    """Core payroll calculation. Returns a dict suitable for preview or saving."""
    emp = Employee.query.get(employee_id)
    if not emp:
        return None

    holidays_this_year = sa_public_holidays(period_start.year)
    if period_end.year != period_start.year:
        holidays_this_year.update(sa_public_holidays(period_end.year))

    # Collect attendance rows in the period
    rows = EmployeeAttendance.query.filter(
        EmployeeAttendance.employee_id == employee_id,
        EmployeeAttendance.work_date >= period_start,
        EmployeeAttendance.work_date <= period_end,
    ).order_by(EmployeeAttendance.work_date).all()

    # Load pay rules
    rules = {r.day_type: Decimal(str(r.multiplier)) for r in PayRule.query.all()}
    def mult(day_type):
        return rules.get(day_type, Decimal('1'))

    rate = Decimal(str(emp.hourly_rate))
    normal_h = Decimal(str(emp.normal_hours_per_day))

    attendance_snapshot = []
    totals = {k: Decimal('0') for k in
              ('normal', 'overtime', 'sunday', 'holiday', 'vacation', 'sick')}
    absent_days = Decimal('0')  # for salaried: days marked absent (no pay)

    is_salaried = (emp.pay_type or 'hourly') == 'salaried'

    # Track normal worked hours per ISO week for the weekly 45h overtime rule
    from collections import defaultdict as _dd
    _week_normal_hrs  = _dd(Decimal)  # (iso_year, iso_week) → sum of actual hours on non-special days
    _week_normal_pool = _dd(Decimal)  # same key → hours currently allocated to totals['normal']

    for a in rows:
        # Recompute from clock in/out when available so the 60-min lunch deduction applies
        # to all records (including those saved before the lunch default was introduced).
        if a.clock_in and a.clock_out:
            computed = _compute_hours(
                a.clock_in.strftime('%H:%M'),
                a.clock_out.strftime('%H:%M'),
                a.break_minutes,
            )
            hrs = Decimal(str(computed)) if computed is not None else Decimal('0')
        else:
            hrs = Decimal(str(a.hours_worked)) if a.hours_worked is not None else Decimal('0')
        dt  = a.day_type

        if dt in ('absent', 'unpaid_leave'):
            # No pay regardless of pay type
            pay_hrs = Decimal('0')
            pay_amt = Decimal('0')
            if dt == 'absent':
                absent_days += Decimal('1')
        elif dt in ('vacation', 'sick'):
            # Paid leave: normal_hours_per_day at 1× rate
            pay_hrs = normal_h
            pay_amt = pay_hrs * rate * mult(dt)
            totals[dt if dt in totals else 'normal'] += pay_hrs
        elif dt == 'sunday':
            pay_hrs = hrs if not is_salaried else max(hrs, normal_h)
            pay_amt = pay_hrs * rate * mult('sunday')
            totals['sunday'] += pay_hrs
        elif dt == 'public_holiday':
            pay_hrs = hrs if hrs > 0 else normal_h
            pay_amt = pay_hrs * rate * mult('public_holiday')
            totals['holiday'] += pay_hrs
        elif is_salaried:
            # Salaried: guaranteed normal_h, overtime on top
            std_hrs = normal_h
            ot_hrs  = max(Decimal('0'), hrs - normal_h)
            pay_amt = std_hrs * rate * mult('normal') + ot_hrs * rate * mult('overtime')
            totals['normal']   += std_hrs
            totals['overtime'] += ot_hrs
            pay_hrs = hrs if hrs > 0 else normal_h
        else:
            # Hourly: pay exactly what's logged
            std_hrs = min(hrs, normal_h)
            ot_hrs  = max(Decimal('0'), hrs - normal_h)
            pay_amt = std_hrs * rate * mult('normal') + ot_hrs * rate * mult('overtime')
            totals['normal']   += std_hrs
            totals['overtime'] += ot_hrs
            pay_hrs = hrs
            # Accumulate for weekly 45h check (only normal working days)
            iso_week = a.work_date.isocalendar()[:2]
            _week_normal_hrs[iso_week]  += hrs
            _week_normal_pool[iso_week] += std_hrs

        # Compute effective break for display — mirrors _compute_hours logic
        if a.clock_in and a.clock_out:
            _raw_min = (a.clock_out.hour * 60 + a.clock_out.minute) - (a.clock_in.hour * 60 + a.clock_in.minute)
            if a.break_minutes and a.break_minutes > 0:
                _eff_break = a.break_minutes
            elif _raw_min > 300:
                _eff_break = 60
            else:
                _eff_break = 0
        else:
            _eff_break = a.break_minutes or 0

        attendance_snapshot.append({
            'date':         a.work_date.isoformat(),
            'clock_in':     a.clock_in.strftime('%H:%M') if a.clock_in else None,
            'clock_out':    a.clock_out.strftime('%H:%M') if a.clock_out else None,
            'break_min':    _eff_break,
            'hours':        float(pay_hrs),
            'day_type':     dt,
            'pay':          float(pay_amt.quantize(Decimal('0.01'))),
        })

    # BCEA weekly overtime: hours >45 in a week on normal working days get 1.5×.
    # Per-day OT (hours > normal_h/day) is already in totals['overtime'].
    # Only reclassify the gap between what per-day OT already caught and the weekly excess.
    _WEEKLY_THRESHOLD = Decimal('45')
    for iso_week, week_total in sorted(_week_normal_hrs.items()):
        if week_total > _WEEKLY_THRESHOLD:
            ot_already = week_total - _week_normal_pool[iso_week]  # per-day OT this week
            weekly_excess = week_total - _WEEKLY_THRESHOLD
            reclassify = min(max(Decimal('0'), weekly_excess - ot_already), _week_normal_pool[iso_week])
            if reclassify > 0:
                totals['normal']   -= reclassify
                totals['overtime'] += reclassify
                # Add a breakdown row for the OT premium so the attendance table sums to gross
                _ot_premium = (reclassify * rate * (mult('overtime') - mult('normal'))).quantize(Decimal('0.01'))
                try:
                    _wk_label = date.fromisocalendar(iso_week[0], iso_week[1], 1).isoformat()
                except Exception:
                    _wk_label = f'Week {iso_week[1]}'
                attendance_snapshot.append({
                    'date':      _wk_label,
                    'clock_in':  None, 'clock_out': None,
                    'break_min': None,
                    'hours':     float(reclassify),
                    'day_type':  'weekly_overtime',
                    'pay':       float(_ot_premium),
                    'note':      f'{float(reclassify):.2f}h exceeded 45h weekly threshold — OT premium',
                })

    # Build shared date sets used by both the public-holiday and salaried loops
    logged_dates = {a.work_date for a in rows}
    absent_dates = {a.work_date for a in rows if a.day_type in ('absent', 'unpaid_leave')}
    try:
        emp_work_days_set = set(int(x) for x in (emp.work_days_json or '0,1,2,3,4,5').split(',') if x.strip())
    except Exception:
        emp_work_days_set = {0, 1, 2, 3, 4, 5}
    holidays_set = set(holidays_this_year.keys())

    # BCEA s18: public holidays on a scheduled work day must be paid at normal rate
    # even when the employee does NOT come in. Worked public holidays are already
    # handled above (day_type='public_holiday' attendance record → 2× via pay rule).
    curr = period_start
    while curr <= period_end:
        if (curr in holidays_set
                and curr.weekday() in emp_work_days_set
                and curr not in logged_dates
                and curr not in absent_dates):
            # Unworked public holiday on a scheduled day → 1× normal pay
            totals['normal'] += normal_h
            attendance_snapshot.append({
                'date':      curr.isoformat(),
                'clock_in':  None, 'clock_out': None, 'break_min': 0,
                'hours':     float(normal_h),
                'day_type':  'public_holiday',
                'pay':       float((normal_h * rate).quantize(Decimal('0.01'))),
                'note':      'Public holiday (not worked) — paid at normal rate',
            })
        curr += timedelta(days=1)

    # For salaried employees: pay ALL expected working days not explicitly marked absent/unpaid
    if is_salaried:
        curr = period_start
        while curr <= period_end:
            if (curr.weekday() in emp_work_days_set
                    and curr not in logged_dates
                    and curr not in holidays_set):
                # Unlogged working day for salaried employee = normal pay
                totals['normal'] += normal_h
                attendance_snapshot.append({
                    'date':      curr.isoformat(),
                    'clock_in':  None, 'clock_out': None, 'break_min': 0,
                    'hours':     float(normal_h),
                    'day_type':  'normal',
                    'pay':       float((normal_h * rate).quantize(Decimal('0.01'))),
                })
            curr += timedelta(days=1)

    # Pay subtotals
    normal_pay   = (totals['normal']   * rate * mult('normal')).quantize(Decimal('0.01'))
    overtime_pay = (totals['overtime'] * rate * mult('overtime')).quantize(Decimal('0.01'))
    sunday_pay   = (totals['sunday']   * rate * mult('sunday')).quantize(Decimal('0.01'))
    holiday_pay  = (totals['holiday']  * rate * mult('public_holiday')).quantize(Decimal('0.01'))
    vacation_pay = (totals['vacation'] * rate * mult('vacation')).quantize(Decimal('0.01'))
    sick_pay     = (totals['sick']     * rate * mult('sick')).quantize(Decimal('0.01'))
    gross        = normal_pay + overtime_pay + sunday_pay + holiday_pay + vacation_pay + sick_pay

    period_days = (period_end - period_start).days + 1

    # Deductions
    deductions   = []
    total_ded    = Decimal('0')
    emp_contribs = []

    has_uif_deduction = False
    for ded in EmployeeDeduction.query.filter_by(employee_id=employee_id, is_active=True).order_by(EmployeeDeduction.sort_order):
        if ded.deduction_type == 'auto_uif':
            amt = _uif_employee(gross, period_days)
            has_uif_deduction = True
        elif ded.deduction_type == 'auto_paye':
            annual = gross * Decimal('26')  # biweekly → annual approximation
            amt = (_paye_annual(annual) / Decimal('26')).quantize(Decimal('0.01'))
        elif ded.deduction_type == 'percentage_of_gross':
            amt = (gross * Decimal(str(ded.amount)) / 100).quantize(Decimal('0.01'))
        else:
            amt = Decimal(str(ded.amount))
        deductions.append({'label': ded.label, 'type': ded.deduction_type, 'amount': float(amt)})
        total_ded += amt

    # UIF employee contribution (1%) — auto-applied if not already in deductions
    if not has_uif_deduction:
        uif_emp = _uif_employee(gross, period_days)
        deductions.append({'label': 'UIF (employee 1%)', 'type': 'auto_uif', 'amount': float(uif_emp)})
        total_ded += uif_emp

    # UIF employer contribution (shown on payslip but not deducted from employee)
    uif_employer = _uif_employer(gross, period_days)
    emp_contribs.append({'label': 'UIF (employer 1%)', 'amount': float(uif_employer)})

    # Outstanding advances to deduct
    advances_due = EmployeeAdvance.query.filter_by(
        employee_id=employee_id, status='outstanding'
    ).all()
    advances_snapshot = []
    for adv in advances_due:
        advances_snapshot.append({'id': adv.id, 'date': adv.date_given.isoformat(),
                                   'reason': adv.reason, 'amount': float(adv.amount)})
        total_ded += Decimal(str(adv.amount))

    # Loan installments
    for loan in EmployeeLoan.query.filter_by(employee_id=employee_id, status='active').all():
        installment = min(Decimal(str(loan.installment)), Decimal(str(loan.balance)))
        if installment > 0:
            deductions.append({'label': f'Loan repayment ({loan.reason or "loan"})',
                               'type': 'loan', 'loan_id': loan.id, 'amount': float(installment)})
            total_ded += installment

    net = gross - total_ded

    return {
        'employee_id':            employee_id,
        'period_start':           period_start.isoformat(),
        'period_end':             period_end.isoformat(),
        'hourly_rate_snapshot':   float(rate),
        'normal_hours':           float(totals['normal']),
        'overtime_hours':         float(totals['overtime']),
        'sunday_hours':           float(totals['sunday']),
        'holiday_hours':          float(totals['holiday']),
        'vacation_hours':         float(totals['vacation']),
        'sick_hours':             float(totals['sick']),
        'normal_pay':             float(normal_pay),
        'overtime_pay':           float(overtime_pay),
        'sunday_pay':             float(sunday_pay),
        'holiday_pay':            float(holiday_pay),
        'vacation_pay':           float(vacation_pay),
        'gross_pay':              float(gross),
        'deductions':             deductions,
        'employer_contributions': emp_contribs,
        'advances':               advances_snapshot,
        'total_deductions':       float(total_ded),
        'net_pay':                float(net),
        'attendance':             attendance_snapshot,
    }

def _next_reference():
    last = db.session.query(func.max(PayRun.id)).scalar() or 0
    return f"PAY-{last + 1:05d}"


# ── Employee CRUD ─────────────────────────────────────────────────────────────

@bp.route('/api/employees', methods=['GET'])
def api_employees_list():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    rows = Employee.query.order_by(Employee.name).all()
    return jsonify([_serialize_employee(e) for e in rows])


@bp.route('/api/employees', methods=['POST'])
def api_employees_create():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    d = request.json or {}
    if not d.get('name', '').strip():
        return jsonify({'error': 'Name required'}), 400
    def _s(val):
        return (val or '').strip() or None
    emp = Employee(
        name                 = d['name'].strip(),
        user_id              = d.get('user_id') or None,
        employee_number      = _s(d.get('employee_number')),
        id_number            = _s(d.get('id_number')),
        tax_number           = _s(d.get('tax_number')),
        uif_number           = _s(d.get('uif_number')),
        bank_name            = _s(d.get('bank_name')),
        bank_account         = _s(d.get('bank_account')),
        bank_branch_code     = _s(d.get('bank_branch_code')),
        phone                = _s(d.get('phone')),
        start_date           = date.fromisoformat(d['start_date']) if d.get('start_date') else None,
        employment_type      = d.get('employment_type', 'permanent'),
        hourly_rate          = Decimal(str(d.get('hourly_rate', 0))),
        normal_hours_per_day = Decimal(str(d.get('normal_hours_per_day', 9))),
        normal_days_per_week = int(d.get('normal_days_per_week', 5)),
        pay_frequency        = d.get('pay_frequency', 'biweekly'),
        pay_day_of_week      = int(d.get('pay_day_of_week', 5)),
        leave_days_per_year  = Decimal(str(d.get('leave_days_per_year', 21))),
        pay_type             = d.get('pay_type', 'hourly'),
        work_days_json       = d.get('work_days_json') or '0,1,2,3,4,5',
        rotation_start_day   = int(d['rotation_start_day']) if d.get('rotation_start_day') is not None and str(d.get('rotation_start_day', '')).strip() != '' else None,
        rotation_slot        = int(d['rotation_slot']) if d.get('rotation_slot') is not None and str(d.get('rotation_slot', '')).strip() != '' else None,
        notes                = _s(d.get('notes')),
        created_by           = current_user().id if current_user() else None,
    )
    db.session.add(emp)
    db.session.flush()
    # Auto-generate employee number if not supplied
    if not emp.employee_number:
        emp.employee_number = f"EMP-{emp.id:04d}"
    db.session.commit()
    return jsonify(_serialize_employee(emp)), 201


@bp.route('/api/employees/<int:eid>', methods=['GET'])
def api_employees_get(eid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    return jsonify(_serialize_employee(emp, include_sensitive=True))


@bp.route('/api/employees/<int:eid>', methods=['PUT'])
def api_employees_update(eid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    emp = Employee.query.get_or_404(eid)
    d = request.json or {}
    for field in ('name', 'employee_number', 'id_number', 'tax_number', 'uif_number',
                  'bank_name', 'bank_account', 'bank_branch_code', 'phone',
                  'employment_type', 'pay_frequency', 'notes'):
        if field in d:
            setattr(emp, field, (d[field] or '').strip() or None)
    if 'name' in d and not d['name'].strip():
        return jsonify({'error': 'Name required'}), 400
    if 'name' in d:
        emp.name = d['name'].strip()
    if 'user_id' in d:
        emp.user_id = d['user_id'] or None
    if 'start_date' in d:
        emp.start_date = date.fromisoformat(d['start_date']) if d['start_date'] else None
    for num_field, default in [('hourly_rate', 0), ('normal_hours_per_day', 9),
                                ('normal_days_per_week', 5), ('pay_day_of_week', 5),
                                ('leave_days_per_year', 21)]:
        if num_field in d:
            setattr(emp, num_field, d[num_field])
    if 'pay_type' in d:
        emp.pay_type = d['pay_type'] or 'hourly'
    if 'work_days_json' in d:
        emp.work_days_json = d['work_days_json'] or '0,1,2,3,4,5'
    if 'rotation_start_day' in d:
        emp.rotation_start_day = int(d['rotation_start_day']) if d['rotation_start_day'] is not None and str(d['rotation_start_day']).strip() != '' else None
    if 'rotation_slot' in d:
        emp.rotation_slot = int(d['rotation_slot']) if d['rotation_slot'] is not None and str(d['rotation_slot']).strip() != '' else None
    if 'is_active' in d:
        emp.is_active = bool(d['is_active'])
    db.session.commit()
    return jsonify(_serialize_employee(emp))


@bp.route('/api/employees/<int:eid>', methods=['DELETE'])
def api_employees_delete(eid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    emp = Employee.query.get_or_404(eid)
    emp.is_active = False
    db.session.commit()
    return jsonify({'ok': True})


# ── Deductions ────────────────────────────────────────────────────────────────

@bp.route('/api/employees/<int:eid>/deductions', methods=['GET'])
def api_deductions_list(eid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    rows = EmployeeDeduction.query.filter_by(employee_id=eid).order_by(EmployeeDeduction.sort_order).all()
    return jsonify([{
        'id': d.id, 'label': d.label, 'deduction_type': d.deduction_type,
        'amount': float(d.amount), 'is_active': d.is_active, 'sort_order': d.sort_order,
    } for d in rows])


@bp.route('/api/employees/<int:eid>/deductions', methods=['POST'])
def api_deductions_create(eid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    Employee.query.get_or_404(eid)
    d = request.json or {}
    ded = EmployeeDeduction(
        employee_id    = eid,
        label          = (d.get('label') or '').strip(),
        deduction_type = d.get('deduction_type', 'fixed'),
        amount         = Decimal(str(d.get('amount', 0))),
        is_active      = d.get('is_active', True),
        sort_order     = d.get('sort_order', 0),
    )
    db.session.add(ded)
    db.session.commit()
    return jsonify({'id': ded.id, 'label': ded.label}), 201


@bp.route('/api/employees/<int:eid>/deductions/<int:did>', methods=['PUT'])
def api_deductions_update(eid, did):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    ded = EmployeeDeduction.query.filter_by(id=did, employee_id=eid).first_or_404()
    d = request.json or {}
    if 'label' in d:          ded.label          = d['label'].strip()
    if 'deduction_type' in d: ded.deduction_type = d['deduction_type']
    if 'amount' in d:         ded.amount         = Decimal(str(d['amount']))
    if 'is_active' in d:      ded.is_active       = bool(d['is_active'])
    if 'sort_order' in d:     ded.sort_order      = int(d['sort_order'])
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/employees/<int:eid>/deductions/<int:did>', methods=['DELETE'])
def api_deductions_delete(eid, did):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    ded = EmployeeDeduction.query.filter_by(id=did, employee_id=eid).first_or_404()
    db.session.delete(ded)
    db.session.commit()
    return jsonify({'ok': True})


# ── Attendance ────────────────────────────────────────────────────────────────

@bp.route('/api/employees/<int:eid>/attendance', methods=['GET'])
def api_attendance_list(eid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    month_str = request.args.get('month')  # YYYY-MM
    if month_str:
        try:
            y, m = map(int, month_str.split('-'))
            d_from = date(y, m, 1)
            d_to   = date(y, m + 1 if m < 12 else 1, 1) - timedelta(days=1) \
                     if m < 12 else date(y + 1, 1, 1) - timedelta(days=1)
        except Exception:
            return jsonify({'error': 'Invalid month (YYYY-MM)'}), 400
    else:
        d_from = date.today().replace(day=1)
        d_to   = date.today()

    rows = EmployeeAttendance.query.filter(
        EmployeeAttendance.employee_id == eid,
        EmployeeAttendance.work_date >= d_from,
        EmployeeAttendance.work_date <= d_to,
    ).order_by(EmployeeAttendance.work_date).all()

    holidays = sa_public_holidays(d_from.year)
    if d_to.year != d_from.year:
        holidays.update(sa_public_holidays(d_to.year))

    return jsonify({
        'attendance': [_serialize_attendance(a) for a in rows],
        'public_holidays': {d.isoformat(): name for d, name in holidays.items()
                            if d_from <= d <= d_to},
    })


@bp.route('/api/employees/generate_schedule', methods=['POST'])
def api_generate_schedule():
    """Bulk-create default attendance for all active employees for a given month.

    rotation=false (default): uses each employee's work_days_json per-employee setting.
    rotation=true: uses global schedule rules (schedule_mandatory_days, schedule_rotation_days,
      schedule_rotation_mode). Each employee's rotation_slot (or alphabetical index) determines
      which day they are off in each week.
    Skips days that already have an entry.
    """
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    d = request.json or {}
    rotation  = bool(d.get('rotation', False))
    # Support both month (YYYY-MM) and explicit date range (date_from / date_to YYYY-MM-DD)
    from datetime import time as dtime
    if d.get('date_from') and d.get('date_to'):
        try:
            month_start = date.fromisoformat(d['date_from'])
            month_end   = date.fromisoformat(d['date_to'])
        except Exception:
            return jsonify({'error': 'date_from and date_to must be YYYY-MM-DD'}), 400
    else:
        month_str = d.get('month')
        try:
            import calendar as _cal
            y, m = map(int, month_str.split('-'))
            month_start = date(y, m, 1)
            month_end   = date(y, m, _cal.monthrange(y, m)[1])
        except Exception:
            return jsonify({'error': 'month required (YYYY-MM) or date_from/date_to'}), 400
    y = month_start.year
    all_days = []
    d_iter = month_start
    while d_iter <= month_end:
        all_days.append(d_iter)
        d_iter += timedelta(days=1)

    employees = Employee.query.filter_by(is_active=True).order_by(Employee.name).all()
    holidays  = sa_public_holidays(y)
    u         = current_user()
    created   = 0
    skipped   = 0
    deleted   = 0

    # Load global schedule rules (used only when rotation=True)
    def _parse_days(key, default):
        raw = get_setting(key) or default
        try:
            return [int(x) for x in raw.split(',') if x.strip() != '']
        except Exception:
            return list(map(int, default.split(',')))

    global_rotation_days  = _parse_days('schedule_rotation_days', '0,1,2,3,4')   # Mon-Fri default
    global_mandatory_days = set(_parse_days('schedule_mandatory_days', '5'))       # Sat default
    rotation_mode         = get_setting('schedule_rotation_mode') or 'fixed'      # fixed | advancing

    # Build rotation slots: use emp.rotation_slot if set, else alphabetical index
    slot_map = {}
    slot_counter = 0
    for emp in sorted(employees, key=lambda e: (e.rotation_slot if e.rotation_slot is not None else 9999, e.name)):
        if emp.rotation_slot is not None:
            slot_map[emp.id] = emp.rotation_slot
        else:
            slot_map[emp.id] = slot_counter
        slot_counter += 1

    # Read default clock-in/out from settings (configurable in Schedule Config)
    def _parse_time_str(s, fallback_h, fallback_m):
        try:
            parts = (s or '').split(':')
            return dtime(int(parts[0]), int(parts[1]))
        except Exception:
            return dtime(fallback_h, fallback_m)

    default_ci_str  = get_setting('schedule_default_clock_in')  or '07:30'
    default_co_str  = get_setting('schedule_default_clock_out') or '17:00'
    default_ci      = _parse_time_str(default_ci_str, 7, 30)
    default_co      = _parse_time_str(default_co_str, 17, 0)
    # Auto-apply 60-min lunch for shifts > 5 h (same rule as _compute_hours)
    _raw_shift_min  = (default_co.hour * 60 + default_co.minute) - (default_ci.hour * 60 + default_ci.minute)
    break_min       = 60 if _raw_shift_min > 300 else 0
    hours           = _compute_hours(default_ci_str, default_co_str, break_min)

    for emp_idx, emp in enumerate(employees):
        clock_in_t  = default_ci
        clock_out_t = default_co

        # Fetch existing entries: separate schedule_default (can be replaced) from manual
        existing_rows = EmployeeAttendance.query.filter(
            EmployeeAttendance.employee_id == emp.id,
            EmployeeAttendance.work_date   >= month_start,
            EmployeeAttendance.work_date   <= month_end,
        ).all()
        existing_schedule = {row.work_date: row for row in existing_rows if row.source == 'schedule_default'}
        existing_manual   = {row.work_date for row in existing_rows if row.source != 'schedule_default'}

        if rotation:
            try:
                emp_work_days = [int(x) for x in (emp.work_days_json or '0,1,2,3,4,5').split(',') if x.strip()]
            except Exception:
                emp_work_days = [0, 1, 2, 3, 4, 5]
            allowed_weekdays = set(emp_work_days)
            rot_pool = [d for d in global_rotation_days if d in allowed_weekdays]
            if not rot_pool:
                rot_pool = list(global_rotation_days)
            slot = slot_map.get(emp.id, emp_idx)
        else:
            try:
                emp_work_days = [int(x) for x in (emp.work_days_json or '0,1,2,3,4,5').split(',') if x.strip()]
            except Exception:
                emp_work_days = [0, 1, 2, 3, 4, 5]
            allowed_weekdays = set(emp_work_days)
            rot_pool = []
            slot = 0

        for day in all_days:
            dow = day.weekday()
            if dow not in allowed_weekdays:
                continue
            # Manual entries are never touched
            if day in existing_manual:
                skipped += 1
                continue

            is_mandatory = dow in global_mandatory_days
            is_off_day   = False

            if rotation and rot_pool and not is_mandatory:
                week_offset = (day.day - 1) // 7
                if rotation_mode == 'advancing':
                    off_idx = (slot + week_offset) % len(rot_pool)
                else:
                    off_idx = slot % len(rot_pool)
                if dow == rot_pool[off_idx]:
                    is_off_day = True

            if is_off_day:
                # Remove any schedule_default entry that already exists for this off-day
                if day in existing_schedule:
                    db.session.delete(existing_schedule[day])
                    deleted += 1
                continue

            if day in existing_schedule:
                skipped += 1
                continue

            day_type = _auto_day_type(day, holidays)
            db.session.add(EmployeeAttendance(
                employee_id   = emp.id,
                work_date     = day,
                clock_in      = clock_in_t,
                clock_out     = clock_out_t,
                break_minutes = break_min,
                hours_worked  = Decimal(str(hours)),
                day_type      = day_type,
                source        = 'schedule_default',
                created_by    = u.id if u else None,
            ))
            created += 1

    db.session.commit()
    return jsonify({'created': created, 'skipped': skipped, 'deleted': deleted, 'employees': len(employees)})


@bp.route('/api/employees/clear_schedule', methods=['POST'])
def api_clear_schedule():
    """Delete all schedule_default attendance entries for a given date range or month.
    Only removes entries with source='schedule_default' — manual/admin entries are untouched.
    """
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    d = request.json or {}
    if d.get('date_from') and d.get('date_to'):
        try:
            d_from = date.fromisoformat(d['date_from'])
            d_to   = date.fromisoformat(d['date_to'])
        except Exception:
            return jsonify({'error': 'date_from and date_to must be YYYY-MM-DD'}), 400
    else:
        month_str = d.get('month')
        try:
            import calendar as _cal
            y, m   = map(int, month_str.split('-'))
            d_from = date(y, m, 1)
            d_to   = date(y, m, _cal.monthrange(y, m)[1])
        except Exception:
            return jsonify({'error': 'month (YYYY-MM) or date_from/date_to required'}), 400

    deleted = (EmployeeAttendance.query
               .filter(EmployeeAttendance.work_date >= d_from,
                       EmployeeAttendance.work_date <= d_to,
                       EmployeeAttendance.source    == 'schedule_default')
               .delete(synchronize_session=False))
    db.session.commit()
    return jsonify({'deleted': deleted, 'from': d_from.isoformat(), 'to': d_to.isoformat()})


@bp.route('/api/employees/attendance/summary', methods=['GET'])
def api_attendance_summary():
    """Returns attendance for ALL active employees for a given month — used by the
    all-employees calendar grid view."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    month_str = request.args.get('month')
    try:
        y, m   = map(int, month_str.split('-'))
        d_from = date(y, m, 1)
    except Exception:
        return jsonify({'error': 'month required (YYYY-MM)'}), 400

    import calendar as _cal
    days_in_month = _cal.monthrange(y, m)[1]
    d_to          = date(y, m, days_in_month)
    all_dates     = [date(y, m, day) for day in range(1, days_in_month + 1)]

    employees = Employee.query.filter_by(is_active=True).order_by(Employee.name).all()
    holidays  = sa_public_holidays(y)

    att_rows = EmployeeAttendance.query.filter(
        EmployeeAttendance.employee_id.in_([e.id for e in employees]),
        EmployeeAttendance.work_date >= d_from,
        EmployeeAttendance.work_date <= d_to,
    ).all()

    att_map = {}
    for row in att_rows:
        att_map.setdefault(row.employee_id, {})[row.work_date.isoformat()] = {
            'hours':     float(row.hours_worked) if row.hours_worked else 0,
            'day_type':  row.day_type,
            'source':    row.source,
            'clock_in':  row.clock_in.strftime('%H:%M')  if row.clock_in  else None,
            'clock_out': row.clock_out.strftime('%H:%M') if row.clock_out else None,
            'id':        row.id,
        }

    return jsonify({
        'employees': [
            {'id': e.id, 'name': e.name, 'days': att_map.get(e.id, {})}
            for e in employees
        ],
        'dates':           [d.isoformat()       for d in all_dates],
        'weekday_labels':  [f"{d.strftime('%a')} {d.day}" for d in all_dates],
        'public_holidays': {
            d.isoformat(): name
            for d, name in holidays.items() if d_from <= d <= d_to
        },
    })


@bp.route('/api/employees/<int:eid>/attendance', methods=['POST'])
def api_attendance_upsert(eid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    Employee.query.get_or_404(eid)
    d = request.json or {}

    try:
        work_date = date.fromisoformat(d['date'])
    except (KeyError, ValueError):
        return jsonify({'error': 'date required (YYYY-MM-DD)'}), 400

    # Auto-detect day type if not provided
    holidays = sa_public_holidays(work_date.year)
    day_type  = d.get('day_type') or _auto_day_type(work_date, holidays)

    # Compute hours if clock in/out provided
    hours = None
    if d.get('clock_in') and d.get('clock_out'):
        hours = _compute_hours(d['clock_in'], d['clock_out'], d.get('break_minutes', 0))
    elif d.get('hours') is not None:
        hours = float(d['hours'])

    existing = EmployeeAttendance.query.filter_by(employee_id=eid, work_date=work_date).first()
    u = current_user()

    if existing:
        existing.clock_in      = d.get('clock_in') or existing.clock_in
        existing.clock_out     = d.get('clock_out') or existing.clock_out
        existing.break_minutes = int(d.get('break_minutes', existing.break_minutes or 0))
        if hours is not None:
            existing.hours_worked = hours
        existing.day_type  = day_type
        existing.notes     = d.get('notes', existing.notes)
        existing.source    = d.get('source', 'admin_entry')
        existing.updated_by = u.id if u else None
        existing.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify(_serialize_attendance(existing))

    row = EmployeeAttendance(
        employee_id   = eid,
        work_date     = work_date,
        clock_in      = _parse_time(d.get('clock_in')),
        clock_out     = _parse_time(d.get('clock_out')),
        break_minutes = int(d.get('break_minutes', 0)),
        hours_worked  = hours,
        day_type      = day_type,
        source        = d.get('source', 'admin_entry'),
        notes         = (d.get('notes') or '').strip() or None,
        created_by    = u.id if u else None,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(_serialize_attendance(row)), 201


@bp.route('/api/employees/<int:eid>/attendance/<int:aid>', methods=['DELETE'])
def api_attendance_delete(eid, aid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    row = EmployeeAttendance.query.filter_by(id=aid, employee_id=eid).first_or_404()
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})


def _parse_time(s):
    if not s:
        return None
    from datetime import time as dtime
    try:
        h, m = map(int, s.split(':'))
        return dtime(h, m)
    except Exception:
        return None


# ── Shift Schedule ────────────────────────────────────────────────────────────

@bp.route('/api/employees/<int:eid>/schedule', methods=['GET'])
def api_schedule_list(eid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    month_str = request.args.get('month')
    if month_str:
        try:
            y, m = map(int, month_str.split('-'))
            d_from = date(y, m, 1)
            d_to   = date(y, m + 1 if m < 12 else 1, 1) - timedelta(days=1) \
                     if m < 12 else date(y + 1, 1, 1) - timedelta(days=1)
        except Exception:
            return jsonify({'error': 'Invalid month'}), 400
    else:
        d_from = date.today().replace(day=1)
        d_to   = date.today()

    rows = ShiftSchedule.query.filter(
        ShiftSchedule.employee_id == eid,
        ShiftSchedule.scheduled_date >= d_from,
        ShiftSchedule.scheduled_date <= d_to,
    ).order_by(ShiftSchedule.scheduled_date).all()

    return jsonify([{
        'id':             r.id,
        'scheduled_date': r.scheduled_date.isoformat(),
        'expected_start': r.expected_start.strftime('%H:%M') if r.expected_start else None,
        'expected_end':   r.expected_end.strftime('%H:%M') if r.expected_end else None,
        'expected_hours': float(r.expected_hours) if r.expected_hours else None,
        'notes':          r.notes,
    } for r in rows])


@bp.route('/api/employees/<int:eid>/schedule', methods=['POST'])
def api_schedule_upsert(eid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    Employee.query.get_or_404(eid)
    d = request.json or {}
    try:
        sched_date = date.fromisoformat(d['date'])
    except (KeyError, ValueError):
        return jsonify({'error': 'date required'}), 400

    exp_hrs = None
    if d.get('expected_start') and d.get('expected_end'):
        exp_hrs = _compute_hours(d['expected_start'], d['expected_end'], d.get('break_minutes', 0))

    existing = ShiftSchedule.query.filter_by(employee_id=eid, scheduled_date=sched_date).first()
    u = current_user()
    if existing:
        existing.expected_start = _parse_time(d.get('expected_start'))
        existing.expected_end   = _parse_time(d.get('expected_end'))
        existing.expected_hours = exp_hrs
        existing.notes          = d.get('notes', existing.notes)
        db.session.commit()
        return jsonify({'id': existing.id})

    row = ShiftSchedule(
        employee_id    = eid,
        scheduled_date = sched_date,
        expected_start = _parse_time(d.get('expected_start')),
        expected_end   = _parse_time(d.get('expected_end')),
        expected_hours = exp_hrs,
        notes          = (d.get('notes') or '').strip() or None,
        created_by     = u.id if u else None,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({'id': row.id}), 201


@bp.route('/api/employees/<int:eid>/schedule/<int:sid>', methods=['DELETE'])
def api_schedule_delete(eid, sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    row = ShiftSchedule.query.filter_by(id=sid, employee_id=eid).first_or_404()
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})


# ── Leave helpers ─────────────────────────────────────────────────────────────

def _annual_cycle_start(emp_start, as_of):
    """Return the start of the current annual leave cycle (hire anniversary)."""
    year = as_of.year
    try:
        cs = date(year, emp_start.month, emp_start.day)
    except ValueError:
        cs = date(year, emp_start.month, 28)
    if cs > as_of:
        year -= 1
        try:
            cs = date(year, emp_start.month, emp_start.day)
        except ValueError:
            cs = date(year, emp_start.month, 28)
    return cs


def _months_diff(d1, d2):
    """Complete months between two dates (d1 <= d2)."""
    return max(0, (d2.year - d1.year) * 12 + d2.month - d1.month)


def _compute_leave_balance(emp, leave_type, as_of=None):
    """
    Compute accrued/used/remaining for one leave type.
    Source of truth for 'used': approved LeaveRequests.
    Returns dict with accrued, used, remaining, policy_label, accrual_method.
    """
    import calendar as _cal
    if as_of is None:
        as_of = date.today()

    policy = LeavePolicy.query.filter_by(leave_type=leave_type).first()
    if not policy:
        return {'accrued': 0.0, 'used': 0.0, 'remaining': 0.0,
                'policy_label': leave_type, 'accrual_method': 'none'}

    # Used: sum approved requests scoped to the correct period.
    # daily (annual): hire-anniversary cycle — BCEA s20 leave cycle.
    # fixed (family_responsibility, custom): calendar year — forfeited Jan 1.
    # sick_cycle: current 36-month cycle — unused days forfeited each cycle.
    # none (maternity, paternal): all-time.
    if policy.accrual_method == 'sick_cycle' and emp.start_date:
        cycle_months_val = int(policy.cycle_months or 36)
        months_employed  = _months_diff(emp.start_date, as_of)
        full_cycles      = months_employed // cycle_months_val
        csm = full_cycles * cycle_months_val
        ys  = emp.start_date.year + (emp.start_date.month - 1 + csm) // 12
        ms  = (emp.start_date.month - 1 + csm) % 12 + 1
        import calendar as _cal
        cycle_start = date(ys, ms, min(emp.start_date.day, _cal.monthrange(ys, ms)[1]))
        used_rows = LeaveRequest.query.filter(
            LeaveRequest.employee_id == emp.id,
            LeaveRequest.leave_type  == leave_type,
            LeaveRequest.status      == 'approved',
            LeaveRequest.date_from   >= cycle_start,
            LeaveRequest.date_from   <= as_of,
        ).all()
    elif policy.accrual_method == 'daily':
        # Scope to hire-anniversary cycle (not calendar year)
        cycle_start_used = _annual_cycle_start(emp.start_date, as_of) if emp.start_date else date(as_of.year, 1, 1)
        used_rows = LeaveRequest.query.filter(
            LeaveRequest.employee_id == emp.id,
            LeaveRequest.leave_type  == leave_type,
            LeaveRequest.status      == 'approved',
            LeaveRequest.date_from   >= cycle_start_used,
            LeaveRequest.date_from   <= as_of,
        ).all()
    elif policy.accrual_method == 'fixed':
        # Calendar-year scoped (family responsibility, custom fixed types)
        yr_start  = date(as_of.year, 1, 1)
        used_rows = LeaveRequest.query.filter(
            LeaveRequest.employee_id == emp.id,
            LeaveRequest.leave_type  == leave_type,
            LeaveRequest.status      == 'approved',
            LeaveRequest.date_from   >= yr_start,
            LeaveRequest.date_from   <= as_of,
        ).all()
    else:
        # 'none' (maternity, paternal) and unrecognised: all-time
        used_rows = LeaveRequest.query.filter(
            LeaveRequest.employee_id == emp.id,
            LeaveRequest.leave_type  == leave_type,
            LeaveRequest.status      == 'approved',
            LeaveRequest.date_from   <= as_of,
        ).all()
    used = float(sum(float(r.days_requested) for r in used_rows))

    if policy.accrual_method == 'none':
        return {'accrued': None, 'used': used, 'remaining': None,
                'policy_label': policy.label, 'accrual_method': 'none'}

    if policy.accrual_method == 'fixed':
        accrued = float(policy.days_per_year or 0)
        if emp.start_date and emp.start_date > as_of:
            accrued = 0.0
        result_accrued = round(accrued, 2)

    elif policy.accrual_method == 'daily':
        # BCEA s20: leave cycle runs from hire anniversary, not Jan 1.
        # Entitlement: explicit emp override > work-week derived > global policy.
        emp_override = float(emp.leave_days_per_year) if emp.leave_days_per_year and float(emp.leave_days_per_year) > 0 else None
        if emp_override is not None:
            days_per_year = emp_override
        elif emp.normal_days_per_week and int(emp.normal_days_per_week) >= 6:
            days_per_year = 18.0  # BCEA: 6-day week → 18 working days
        else:
            days_per_year = float(policy.days_per_year or 15)

        if emp.start_date is None or emp.start_date > as_of:
            result_accrued = 0.0
        else:
            cycle_start = _annual_cycle_start(emp.start_date, as_of)
            earn_start  = max(cycle_start, emp.start_date)
            days_earned = max(0, (as_of - earn_start).days + 1)
            accrued_this_cycle = days_per_year * days_earned / 365.0
            carry_over = 0.0
            carry_over_max_val = float(policy.carry_over_max or 0)
            if carry_over_max_val > 0 and cycle_start > emp.start_date:
                prev_cycle_end = cycle_start - timedelta(days=1)
                prev = _compute_leave_balance(emp, leave_type, prev_cycle_end)
                carry_over = min(float(prev.get('remaining') or 0), carry_over_max_val)
            result_accrued = round(accrued_this_cycle + carry_over, 2)

    elif policy.accrual_method == 'sick_cycle':
        if emp.start_date is None or emp.start_date > as_of:
            result_accrued = 0.0
        else:
            months_employed = _months_diff(emp.start_date, as_of)
            cycle_months = int(policy.cycle_months or 36)
            cycle_days   = float(policy.cycle_days or 30)
            full_cycles  = months_employed // cycle_months
            # Full entitlement upfront: grant full cycle_days for every cycle
            # including the current (in-progress) one from day 1 of employment.
            result_accrued = round((full_cycles + 1) * cycle_days, 2)
    else:
        result_accrued = 0.0

    # ── Add leave adjustments (admin awards / deductions) ─────────────────────
    if policy.accrual_method != 'none':
        adj_filter = [
            EmployeeLeaveAdjustment.employee_id == emp.id,
            EmployeeLeaveAdjustment.leave_type  == leave_type,
        ]
        if policy.accrual_method == 'daily':
            # Year-scoped: apply adjustments with year=NULL or year=this year
            adj_filter.append(
                db.or_(EmployeeLeaveAdjustment.year == None,
                       EmployeeLeaveAdjustment.year == as_of.year)
            )
        # sick_cycle and fixed: apply all adjustments (permanent modifiers)
        adj_rows = EmployeeLeaveAdjustment.query.filter(*adj_filter).all()
        adjustment_total = float(sum(float(a.adjustment_days) for a in adj_rows))
        result_accrued = round(result_accrued + adjustment_total, 2)
    else:
        adj_rows = []
        adjustment_total = 0.0

    remaining = round(max(0.0, result_accrued - used), 2)
    emp_days_override = float(emp.leave_days_per_year) if policy.accrual_method == 'daily' and emp.leave_days_per_year and float(emp.leave_days_per_year) > 0 else None
    out = {
        'accrued':            result_accrued,
        'used':               round(used, 2),
        'remaining':          remaining,
        'adjustment_total':   round(adjustment_total, 2),
        'policy_label':       policy.label,
        'accrual_method':     policy.accrual_method,
        'days_per_year':      emp_days_override if emp_days_override else (float(policy.days_per_year) if policy.days_per_year else None),
        'days_per_year_is_override': emp_days_override is not None,
    }
    if policy.accrual_method == 'sick_cycle':
        out['cycle_days']   = float(policy.cycle_days or 30)
        out['cycle_months'] = int(policy.cycle_months or 36)
    return out


# ── Leave ─────────────────────────────────────────────────────────────────────

@bp.route('/api/employees/<int:eid>/leaves', methods=['GET'])
def api_leaves_list(eid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    rows = LeaveRequest.query.filter_by(employee_id=eid).order_by(LeaveRequest.date_from.desc()).all()
    return jsonify([{
        'id':            r.id,
        'leave_type':    r.leave_type,
        'date_from':     r.date_from.isoformat(),
        'date_to':       r.date_to.isoformat(),
        'days_requested':float(r.days_requested),
        'reason':        r.reason,
        'status':        r.status,
        'approved_at':   r.approved_at.isoformat() if r.approved_at else None,
        'rejection_reason': r.rejection_reason,
        'has_document':  bool(r.document_filename),
        'created_at':    r.created_at.isoformat(),
    } for r in rows])


@bp.route('/api/employees/<int:eid>/leaves', methods=['POST'])
def api_leaves_request(eid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    d = request.json or {}
    try:
        d_from = date.fromisoformat(d['date_from'])
        d_to   = date.fromisoformat(d['date_to'])
    except (KeyError, ValueError):
        return jsonify({'error': 'date_from and date_to required (YYYY-MM-DD)'}), 400

    leave_type = d.get('leave_type', 'annual')
    reason     = (d.get('reason') or '').strip() or None
    policy     = LeavePolicy.query.filter_by(leave_type=leave_type).first()

    # Family responsibility eligibility check (BCEA s27)
    if leave_type == 'family_responsibility':
        if not reason:
            return jsonify({'error': 'Reason required for family responsibility leave (e.g. child sick, bereavement)'}), 400
        if emp.start_date and (date.today() - emp.start_date).days < 120:
            return jsonify({'error': 'Family responsibility leave requires more than 4 months of employment'}), 400
        if emp.normal_days_per_week and int(emp.normal_days_per_week) <= 4:
            return jsonify({'error': 'Family responsibility leave requires working more than 4 days per week'}), 400

    # days_requested: count only actual work days (Mon–Fri or emp schedule), excluding public holidays
    ph = sa_public_holidays(d_from.year)
    if d_to.year != d_from.year:
        ph.update(sa_public_holidays(d_to.year))
    try:
        emp_work_days = set(int(x) for x in (emp.work_days_json or '0,1,2,3,4,5').split(',') if x.strip())
    except Exception:
        emp_work_days = {0, 1, 2, 3, 4, 5}
    working_days = Decimal('0')
    cur = d_from
    while cur <= d_to:
        if cur.weekday() in emp_work_days and cur not in ph:
            working_days += Decimal('1')
        cur += timedelta(days=1)
    days = Decimal(str(d.get('days_requested'))) if d.get('days_requested') else working_days

    # Maternity/paternal: check against policy duration cap
    if policy and policy.days_per_year and days > Decimal(str(policy.days_per_year)):
        return jsonify({'error': f'{policy.label} is limited to {policy.days_per_year} days'}), 400

    req = LeaveRequest(
        employee_id    = eid,
        leave_type     = leave_type,
        date_from      = d_from,
        date_to        = d_to,
        days_requested = days,
        reason         = reason,
    )
    db.session.add(req)
    db.session.commit()
    return jsonify({'id': req.id, 'status': req.status, 'days_requested': float(days)}), 201


@bp.route('/api/employees/<int:eid>/leaves/<int:lid>/approve', methods=['PUT'])
def api_leaves_approve(eid, lid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    req = LeaveRequest.query.filter_by(id=lid, employee_id=eid).first_or_404()
    u   = current_user()
    req.status      = 'approved'
    req.approved_by = u.id if u else None
    req.approved_at = datetime.utcnow()

    # Auto-create attendance records so _calculate_pay_run sees these days.
    # Paid leave (vacation/sick) → day_type matches the leave type so payroll pays them.
    # Unpaid leave → day_type='unpaid_leave' so payroll deducts for salaried employees.
    policy = LeavePolicy.query.filter_by(leave_type=req.leave_type).first()
    emp    = Employee.query.get(eid)
    leave_day_type = (
        'vacation'     if req.leave_type == 'annual' else
        'sick'         if req.leave_type == 'sick'   else
        'unpaid_leave' if (policy and not policy.is_paid) else
        'vacation'
    )
    pay_hrs = Decimal(str(emp.normal_hours_per_day or 8)) if leave_day_type != 'unpaid_leave' else Decimal('0')
    try:
        emp_work_days = set(int(x) for x in (emp.work_days_json or '0,1,2,3,4,5').split(',') if x.strip())
    except Exception:
        emp_work_days = {0, 1, 2, 3, 4, 5}
    ph = sa_public_holidays(req.date_from.year)
    if req.date_to.year != req.date_from.year:
        ph.update(sa_public_holidays(req.date_to.year))

    # Medical certificate warning for sick leave > threshold
    warnings = []
    if req.leave_type == 'sick' and policy and policy.requires_proof_after_days:
        if float(req.days_requested) > float(policy.requires_proof_after_days):
            warnings.append(f'Medical certificate required (absence > {policy.requires_proof_after_days} days)')

    current_day = req.date_from
    while current_day <= req.date_to:
        if current_day.weekday() in emp_work_days:
            if current_day in ph:
                # BCEA s20: public holiday during annual leave = employee gets an extra day
                # Don't create a leave attendance record; the payroll loop auto-pays this day.
                pass
            else:
                existing = EmployeeAttendance.query.filter_by(
                    employee_id=eid, work_date=current_day
                ).first()
                if not existing:
                    db.session.add(EmployeeAttendance(
                        employee_id  = eid,
                        work_date    = current_day,
                        day_type     = leave_day_type,
                        hours_worked = pay_hrs,
                        notes        = f'{req.leave_type.replace("_"," ").title()} (approved)',
                    ))
        current_day += timedelta(days=1)

    db.session.commit()
    return jsonify({'ok': True, 'status': 'approved', 'warnings': warnings})


@bp.route('/api/employees/<int:eid>/leaves/<int:lid>/reject', methods=['PUT'])
def api_leaves_reject(eid, lid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    req = LeaveRequest.query.filter_by(id=lid, employee_id=eid).first_or_404()
    req.status           = 'rejected'
    req.rejection_reason = ((request.json or {}).get('reason') or '').strip() or None
    db.session.commit()
    return jsonify({'ok': True, 'status': 'rejected'})


# ── Advances ──────────────────────────────────────────────────────────────────

@bp.route('/api/employees/<int:eid>/advances', methods=['GET'])
def api_advances_list(eid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    rows = EmployeeAdvance.query.filter_by(employee_id=eid).order_by(EmployeeAdvance.date_given.desc()).all()
    return jsonify([{
        'id':         r.id,
        'amount':     float(r.amount),
        'date_given': r.date_given.isoformat(),
        'reason':     r.reason,
        'status':     r.status,
        'pay_run_id': r.pay_run_id,
        'created_at': r.created_at.isoformat(),
    } for r in rows])


@bp.route('/api/employees/<int:eid>/advances', methods=['POST'])
def api_advances_create(eid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    Employee.query.get_or_404(eid)
    d = request.json or {}
    u = current_user()
    adv = EmployeeAdvance(
        employee_id = eid,
        amount      = Decimal(str(d.get('amount', 0))),
        date_given  = date.fromisoformat(d['date_given']) if d.get('date_given') else date.today(),
        reason      = (d.get('reason') or '').strip() or None,
        approved_by = u.id if u else None,
    )
    db.session.add(adv)
    db.session.commit()
    return jsonify({'id': adv.id, 'amount': float(adv.amount)}), 201


@bp.route('/api/employees/<int:eid>/advances/<int:aid>/cancel', methods=['PUT'])
def api_advances_cancel(eid, aid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    adv = EmployeeAdvance.query.filter_by(id=aid, employee_id=eid, status='outstanding').first_or_404()
    adv.status = 'cancelled'
    db.session.commit()
    return jsonify({'ok': True})


# ── Loans ─────────────────────────────────────────────────────────────────────

@bp.route('/api/employees/<int:eid>/loans', methods=['GET'])
def api_loans_list(eid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    rows = EmployeeLoan.query.filter_by(employee_id=eid).order_by(EmployeeLoan.date_given.desc()).all()
    return jsonify([{
        'id':          r.id,
        'principal':   float(r.principal),
        'balance':     float(r.balance),
        'installment': float(r.installment),
        'date_given':  r.date_given.isoformat(),
        'reason':      r.reason,
        'status':      r.status,
        'paid':        float(r.principal - r.balance),
    } for r in rows])


@bp.route('/api/employees/<int:eid>/loans', methods=['POST'])
def api_loans_create(eid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    Employee.query.get_or_404(eid)
    d = request.json or {}
    u = current_user()
    principal = Decimal(str(d.get('amount', 0)))
    loan = EmployeeLoan(
        employee_id = eid,
        principal   = principal,
        balance     = principal,
        installment = Decimal(str(d.get('installment', 0))),
        date_given  = date.fromisoformat(d['date_given']) if d.get('date_given') else date.today(),
        reason      = (d.get('reason') or '').strip() or None,
        approved_by = u.id if u else None,
    )
    db.session.add(loan)
    db.session.commit()
    return jsonify({'id': loan.id}), 201


# ── Documents ─────────────────────────────────────────────────────────────────

@bp.route('/api/employees/<int:eid>/documents', methods=['GET'])
def api_documents_list(eid):
    _, _, err = _own_or_admin(eid)
    if err:
        return err
    rows = EmployeeDocument.query.filter_by(employee_id=eid).order_by(EmployeeDocument.uploaded_at.desc()).all()
    return jsonify([{
        'id':            r.id,
        'document_type': r.document_type,
        'label':         r.label,
        'original_name': r.original_name,
        'uploaded_at':   r.uploaded_at.isoformat(),
    } for r in rows])


@bp.route('/api/employees/<int:eid>/documents', methods=['POST'])
def api_documents_upload(eid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    Employee.query.get_or_404(eid)
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    doc_type = request.form.get('document_type', 'other')
    label    = request.form.get('label', f.filename).strip()
    import uuid as _uuid
    ext      = os.path.splitext(f.filename)[1].lower()
    filename = f"{_uuid.uuid4().hex}{ext}"
    os.makedirs(EMPLOYEE_DOCS_DIR, exist_ok=True)
    f.save(os.path.join(EMPLOYEE_DOCS_DIR, filename))
    u   = current_user()
    doc = EmployeeDocument(
        employee_id   = eid,
        document_type = doc_type,
        label         = label,
        filename      = filename,
        original_name = f.filename,
        uploaded_by   = u.id if u else None,
    )
    db.session.add(doc)
    db.session.commit()
    return jsonify({'id': doc.id, 'label': doc.label}), 201


@bp.route('/api/employees/<int:eid>/documents/<int:did>/download', methods=['GET'])
def api_documents_download(eid, did):
    _, _, err = _own_or_admin(eid)
    if err:
        return err
    doc  = EmployeeDocument.query.filter_by(id=did, employee_id=eid).first_or_404()
    path = os.path.join(EMPLOYEE_DOCS_DIR, doc.filename)
    from flask import send_file
    return send_file(path, download_name=doc.original_name, as_attachment=True)


@bp.route('/api/employees/<int:eid>/documents/<int:did>', methods=['DELETE'])
def api_documents_delete(eid, did):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    doc  = EmployeeDocument.query.filter_by(id=did, employee_id=eid).first_or_404()
    path = os.path.join(EMPLOYEE_DOCS_DIR, doc.filename)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})


# ── Pay Runs ──────────────────────────────────────────────────────────────────

@bp.route('/api/employees/<int:eid>/pay_runs', methods=['GET'])
def api_pay_runs_list(eid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    rows = PayRun.query.filter_by(employee_id=eid).order_by(PayRun.period_start.desc()).all()
    return jsonify([_serialize_pay_run(r, brief=True) for r in rows])


@bp.route('/api/employees/<int:eid>/pay_runs/preview', methods=['POST'])
def api_pay_runs_preview(eid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    Employee.query.get_or_404(eid)
    d = request.json or {}
    try:
        p_start = date.fromisoformat(d['period_start'])
        p_end   = date.fromisoformat(d['period_end'])
    except (KeyError, ValueError):
        return jsonify({'error': 'period_start and period_end required'}), 400
    result = _calculate_pay_run(eid, p_start, p_end)
    if not result:
        return jsonify({'error': 'Calculation failed'}), 500
    return jsonify(result)


@bp.route('/api/employees/<int:eid>/pay_runs', methods=['POST'])
def api_pay_runs_create(eid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    emp = Employee.query.get_or_404(eid)
    d   = request.json or {}
    try:
        p_start = date.fromisoformat(d['period_start'])
        p_end   = date.fromisoformat(d['period_end'])
    except (KeyError, ValueError):
        return jsonify({'error': 'period_start and period_end required'}), 400

    calc = _calculate_pay_run(eid, p_start, p_end)
    if not calc:
        return jsonify({'error': 'Calculation failed'}), 500

    u   = current_user()
    ref = _next_reference()
    pr  = PayRun(
        reference                  = ref,
        employee_id                = eid,
        period_start               = p_start,
        period_end                 = p_end,
        pay_date                   = date.fromisoformat(d['pay_date']) if d.get('pay_date') else None,
        hourly_rate_snapshot       = Decimal(str(calc['hourly_rate_snapshot'])),
        normal_hours               = Decimal(str(calc['normal_hours'])),
        overtime_hours             = Decimal(str(calc['overtime_hours'])),
        sunday_hours               = Decimal(str(calc['sunday_hours'])),
        holiday_hours              = Decimal(str(calc['holiday_hours'])),
        vacation_hours             = Decimal(str(calc['vacation_hours'])),
        sick_hours                 = Decimal(str(calc['sick_hours'])),
        normal_pay                 = Decimal(str(calc['normal_pay'])),
        overtime_pay               = Decimal(str(calc['overtime_pay'])),
        sunday_pay                 = Decimal(str(calc['sunday_pay'])),
        holiday_pay                = Decimal(str(calc['holiday_pay'])),
        vacation_pay               = Decimal(str(calc['vacation_pay'])),
        gross_pay                  = Decimal(str(calc['gross_pay'])),
        deductions_json            = json.dumps(calc['deductions']),
        employer_contributions_json = json.dumps(calc['employer_contributions']),
        advances_json              = json.dumps(calc['advances']),
        attendance_json            = json.dumps(calc['attendance']),
        total_deductions           = Decimal(str(calc['total_deductions'])),
        net_pay                    = Decimal(str(calc['net_pay'])),
        notes                      = (d.get('notes') or '').strip() or None,
        created_by                 = u.id if u else None,
    )
    db.session.add(pr)
    db.session.commit()
    return jsonify(_serialize_pay_run(pr)), 201


@bp.route('/api/employees/<int:eid>/pay_runs/<int:pid>/approve', methods=['PUT'])
def api_pay_runs_approve(eid, pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    pr = PayRun.query.filter_by(id=pid, employee_id=eid).first_or_404()
    if pr.status != 'draft':
        return jsonify({'error': f'Cannot approve a {pr.status} pay run'}), 400
    u              = current_user()
    pr.status      = 'approved'
    pr.approved_by = u.id if u else None
    pr.approved_at = datetime.utcnow()

    # Mark advances as deducted
    for adv_snap in json.loads(pr.advances_json):
        adv = EmployeeAdvance.query.get(adv_snap['id'])
        if adv and adv.status == 'outstanding':
            adv.status     = 'deducted'
            adv.pay_run_id = pr.id

    # Reduce loan balances
    for ded in json.loads(pr.deductions_json):
        if ded.get('type') == 'loan' and ded.get('loan_id'):
            loan = EmployeeLoan.query.get(ded['loan_id'])
            if loan and loan.status == 'active':
                loan.balance = max(Decimal('0'), Decimal(str(loan.balance)) - Decimal(str(ded['amount'])))
                if loan.balance == 0:
                    loan.status = 'settled'

    db.session.commit()
    return jsonify({'ok': True, 'status': 'approved', 'reference': pr.reference})


@bp.route('/api/employees/<int:eid>/pay_runs/<int:pid>', methods=['DELETE'])
def api_pay_runs_delete(eid, pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    pr = PayRun.query.filter_by(id=pid, employee_id=eid).first_or_404()
    if pr.status == 'paid':
        return jsonify({'error': 'Cannot delete a paid pay run'}), 400
    # Reverse advance deductions if the run was approved
    if pr.status == 'approved':
        for adv_snap in json.loads(pr.advances_json or '[]'):
            adv = EmployeeAdvance.query.get(adv_snap.get('id'))
            if adv and adv.status == 'deducted' and adv.pay_run_id == pr.id:
                adv.status     = 'outstanding'
                adv.pay_run_id = None
        for ded in json.loads(pr.deductions_json or '[]'):
            if ded.get('type') == 'loan' and ded.get('loan_id'):
                loan = EmployeeLoan.query.get(ded['loan_id'])
                if loan:
                    loan.balance = Decimal(str(loan.balance)) + Decimal(str(ded['amount']))
                    if loan.status == 'settled':
                        loan.status = 'active'
    db.session.delete(pr)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/employees/pay_runs/bulk', methods=['POST'])
def api_pay_runs_bulk():
    """Create draft pay runs for all (or selected) active employees for the same period."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    d = request.json or {}
    try:
        p_start = date.fromisoformat(d['period_start'])
        p_end   = date.fromisoformat(d['period_end'])
    except (KeyError, ValueError):
        return jsonify({'error': 'period_start and period_end required'}), 400
    pay_date_val = date.fromisoformat(d['pay_date']) if d.get('pay_date') else None
    notes_val    = (d.get('notes') or '').strip() or None
    emp_ids      = d.get('employee_ids')  # list of ints, or None/missing = all active

    if emp_ids:
        employees = Employee.query.filter(Employee.id.in_(emp_ids), Employee.is_active == True).all()
    else:
        employees = Employee.query.filter_by(is_active=True).order_by(Employee.name).all()

    u       = current_user()
    created = []
    skipped = []
    errors  = []

    for emp in employees:
        # Skip if a pay run for this exact period already exists
        existing = PayRun.query.filter_by(
            employee_id  = emp.id,
            period_start = p_start,
            period_end   = p_end,
        ).first()
        if existing:
            skipped.append({'id': emp.id, 'name': emp.name, 'reason': 'already exists'})
            continue

        calc = _calculate_pay_run(emp.id, p_start, p_end)
        if not calc:
            errors.append({'id': emp.id, 'name': emp.name, 'reason': 'calculation failed'})
            continue

        ref = _next_reference()
        pr  = PayRun(
            reference                   = ref,
            employee_id                 = emp.id,
            period_start                = p_start,
            period_end                  = p_end,
            pay_date                    = pay_date_val,
            hourly_rate_snapshot        = Decimal(str(calc['hourly_rate_snapshot'])),
            normal_hours                = Decimal(str(calc['normal_hours'])),
            overtime_hours              = Decimal(str(calc['overtime_hours'])),
            sunday_hours                = Decimal(str(calc['sunday_hours'])),
            holiday_hours               = Decimal(str(calc['holiday_hours'])),
            vacation_hours              = Decimal(str(calc['vacation_hours'])),
            sick_hours                  = Decimal(str(calc['sick_hours'])),
            normal_pay                  = Decimal(str(calc['normal_pay'])),
            overtime_pay                = Decimal(str(calc['overtime_pay'])),
            sunday_pay                  = Decimal(str(calc['sunday_pay'])),
            holiday_pay                 = Decimal(str(calc['holiday_pay'])),
            vacation_pay                = Decimal(str(calc['vacation_pay'])),
            gross_pay                   = Decimal(str(calc['gross_pay'])),
            deductions_json             = json.dumps(calc['deductions']),
            employer_contributions_json = json.dumps(calc['employer_contributions']),
            advances_json               = json.dumps(calc['advances']),
            attendance_json             = json.dumps(calc['attendance']),
            total_deductions            = Decimal(str(calc['total_deductions'])),
            net_pay                     = Decimal(str(calc['net_pay'])),
            notes                       = notes_val,
            created_by                  = u.id if u else None,
        )
        db.session.add(pr)
        db.session.flush()
        created.append({
            'id': emp.id, 'name': emp.name, 'reference': ref,
            'gross': float(calc['gross_pay']), 'net': float(calc['net_pay']),
        })

    db.session.commit()
    return jsonify({'created': created, 'skipped': skipped, 'errors': errors}), 201


@bp.route('/api/employees/schedule_rules', methods=['GET'])
def api_schedule_rules_get():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify({
        'mandatory_days':  get_setting('schedule_mandatory_days')   or '5',
        'rotation_days':   get_setting('schedule_rotation_days')    or '0,1,2,3,4',
        'rotation_mode':   get_setting('schedule_rotation_mode')    or 'fixed',
        'default_clock_in':  get_setting('schedule_default_clock_in')  or '07:30',
        'default_clock_out': get_setting('schedule_default_clock_out') or '17:00',
    })


@bp.route('/api/employees/schedule_rules', methods=['POST'])
def api_schedule_rules_save():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    d = request.json or {}
    def _save(key, val):
        s = Setting.query.filter_by(key=key).first()
        if s:
            s.value = val
        else:
            db.session.add(Setting(key=key, value=val))
    if 'mandatory_days' in d:
        _save('schedule_mandatory_days', d['mandatory_days'])
    if 'rotation_days' in d:
        _save('schedule_rotation_days', d['rotation_days'])
    if 'rotation_mode' in d:
        _save('schedule_rotation_mode', d['rotation_mode'])
    if 'default_clock_in' in d:
        _save('schedule_default_clock_in', d['default_clock_in'])
    if 'default_clock_out' in d:
        _save('schedule_default_clock_out', d['default_clock_out'])
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/employees/<int:eid>/pay_runs/<int:pid>/paid', methods=['PUT'])
def api_pay_runs_paid(eid, pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    pr = PayRun.query.filter_by(id=pid, employee_id=eid).first_or_404()
    if pr.status not in ('approved', 'paid'):
        return jsonify({'error': 'Pay run must be approved first'}), 400
    pr.status  = 'paid'
    pr.paid_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'status': 'paid'})


@bp.route('/api/employees/<int:eid>/leave_balance', methods=['GET'])
def api_leave_balance(eid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    as_of    = date.today()
    policies = LeavePolicy.query.order_by(LeavePolicy.sort_order).all()
    result   = {}
    for p in policies:
        result[p.leave_type] = _compute_leave_balance(emp, p.leave_type, as_of)
    return jsonify(result)


@bp.route('/api/employees/leave_policies', methods=['GET'])
def api_leave_policies_list():
    if not require_login():
        return jsonify({'error': 'Login required'}), 401
    policies = LeavePolicy.query.order_by(LeavePolicy.sort_order).all()
    return jsonify([{
        'leave_type':                p.leave_type,
        'label':                     p.label,
        'accrual_method':            p.accrual_method,
        'days_per_year':             float(p.days_per_year) if p.days_per_year else None,
        'carry_over_max':            float(p.carry_over_max),
        'cycle_days':                float(p.cycle_days) if p.cycle_days else None,
        'cycle_months':              p.cycle_months,
        'first_period_months':       p.first_period_months,
        'first_period_per_26':       p.first_period_per_26,
        'requires_proof_after_days': p.requires_proof_after_days,
        'is_paid':                   p.is_paid,
        'sort_order':                p.sort_order,
    } for p in policies])


@bp.route('/api/employees/leave_policies/<leave_type>', methods=['PUT'])
def api_leave_policy_update(leave_type):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = LeavePolicy.query.filter_by(leave_type=leave_type).first_or_404()
    d = request.json or {}
    if 'label'                     in d: p.label                     = d['label']
    if 'days_per_year'             in d: p.days_per_year             = d['days_per_year']
    if 'carry_over_max'            in d: p.carry_over_max            = d['carry_over_max']
    if 'cycle_days'                in d: p.cycle_days                = d['cycle_days']
    if 'cycle_months'              in d: p.cycle_months              = d['cycle_months']
    if 'first_period_months'       in d: p.first_period_months       = d['first_period_months']
    if 'first_period_per_26'       in d: p.first_period_per_26       = bool(d['first_period_per_26'])
    if 'requires_proof_after_days' in d: p.requires_proof_after_days = d['requires_proof_after_days']
    if 'is_paid'                   in d: p.is_paid                   = bool(d['is_paid'])
    db.session.commit()
    return jsonify({'ok': True})


_BUILTIN_LEAVE_TYPES = {'annual', 'sick', 'family_responsibility', 'unpaid'}


@bp.route('/api/employees/leave_policies', methods=['POST'])
def api_leave_policy_create():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    d = request.json or {}
    leave_type = (d.get('leave_type') or '').strip().lower().replace(' ', '_')
    label      = (d.get('label') or '').strip()
    if not leave_type or not label:
        return jsonify({'error': 'leave_type and label are required'}), 400
    if LeavePolicy.query.filter_by(leave_type=leave_type).first():
        return jsonify({'error': f'Leave type "{leave_type}" already exists'}), 409
    p = LeavePolicy(
        leave_type     = leave_type,
        label          = label,
        accrual_method = d.get('accrual_method', 'fixed'),
        days_per_year  = d.get('days_per_year'),
        is_paid        = bool(d.get('is_paid', True)),
        sort_order     = 99,
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({'ok': True, 'leave_type': leave_type}), 201


@bp.route('/api/employees/leave_policies/<leave_type>', methods=['DELETE'])
def api_leave_policy_delete(leave_type):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    if leave_type in _BUILTIN_LEAVE_TYPES:
        return jsonify({'error': 'Built-in leave types cannot be deleted'}), 400
    p = LeavePolicy.query.filter_by(leave_type=leave_type).first_or_404()
    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/employees/<int:eid>/leaves/<int:lid>', methods=['DELETE'])
def api_leaves_cancel(eid, lid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    req = LeaveRequest.query.filter_by(id=lid, employee_id=eid).first_or_404()
    if req.status != 'requested':
        return jsonify({'error': 'Only pending requests can be cancelled'}), 400
    db.session.delete(req)
    db.session.commit()
    return jsonify({'ok': True})


# ── Leave Adjustments (per-employee bonus/deduction days) ─────────────────────

@bp.route('/api/employees/<int:eid>/leave_adjustments', methods=['GET'])
def api_leave_adjustments_list(eid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    rows = EmployeeLeaveAdjustment.query.filter_by(employee_id=eid).order_by(
        EmployeeLeaveAdjustment.created_at.desc()).all()
    return jsonify([{
        'id':              r.id,
        'leave_type':      r.leave_type,
        'adjustment_days': float(r.adjustment_days),
        'year':            r.year,
        'reason':          r.reason,
        'created_at':      r.created_at.isoformat(),
    } for r in rows])


@bp.route('/api/employees/<int:eid>/leave_adjustments', methods=['POST'])
def api_leave_adjustments_create(eid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    emp = Employee.query.get_or_404(eid)
    d   = request.json or {}
    try:
        days = Decimal(str(d['adjustment_days']))
    except (KeyError, Exception):
        return jsonify({'error': 'adjustment_days required'}), 400
    u = current_user()
    adj = EmployeeLeaveAdjustment(
        employee_id     = eid,
        leave_type      = d.get('leave_type', 'annual'),
        adjustment_days = days,
        year            = int(d['year']) if d.get('year') else None,
        reason          = (d.get('reason') or '').strip() or None,
        created_by      = u.id if u else None,
    )
    db.session.add(adj)
    db.session.commit()
    return jsonify({'id': adj.id}), 201


@bp.route('/api/employees/<int:eid>/leave_adjustments/<int:aid>', methods=['DELETE'])
def api_leave_adjustments_delete(eid, aid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    adj = EmployeeLeaveAdjustment.query.filter_by(id=aid, employee_id=eid).first_or_404()
    db.session.delete(adj)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/employees/<int:eid>/pay_runs/<int:pid>/payslip', methods=['GET'])
def api_payslip(eid, pid):
    emp, is_own, err = _own_or_admin(eid)
    if err:
        return err
    pr = PayRun.query.filter_by(id=pid, employee_id=eid).first_or_404()

    approved_by_name = ''
    if pr.approved_by:
        ab = User.query.get(pr.approved_by)
        approved_by_name = ab.username if ab else ''

    store_name = get_setting('branding_store_name') or 'Lady Coleen Boutique Farmstall'

    # YTD totals
    year_start = date(pr.period_end.year, 1, 1)
    ytd_runs   = PayRun.query.filter(
        PayRun.employee_id == eid,
        PayRun.period_end  >= year_start,
        PayRun.period_end  <= pr.period_end,
        PayRun.status.in_(['approved', 'paid']),
        PayRun.id          <= pr.id,
    ).all()
    ytd_gross = sum(float(r.gross_pay) for r in ytd_runs)
    ytd_net   = sum(float(r.net_pay)   for r in ytd_runs)
    ytd_vacation_hours   = sum(float(r.vacation_hours) for r in ytd_runs)
    ytd_sick_hours       = sum(float(r.sick_hours) for r in ytd_runs)
    hrs_per_day          = float(emp.normal_hours_per_day) if float(emp.normal_hours_per_day) > 0 else 8
    ytd_vacation_days    = round(ytd_vacation_hours / hrs_per_day, 2)
    ytd_sick_days        = round(ytd_sick_hours     / hrs_per_day, 2)

    # Compute leave balances for all leave types using the authoritative function
    all_policies = LeavePolicy.query.order_by(LeavePolicy.sort_order).all()
    as_of_date   = pr.period_end
    leave_balances = []
    for pol in all_policies:
        try:
            bal = _compute_leave_balance(emp, pol.leave_type, as_of_date)
        except Exception:
            bal = {'accrued': 0, 'used': 0, 'remaining': 0}
        leave_balances.append({
            'label':     pol.label,
            'accrued':   round(float(bal.get('accrued', 0) or 0), 1),
            'used':      round(float(bal.get('used',    0) or 0), 1),
            'remaining': round(float(bal.get('remaining',0) or 0), 1),
            'is_paid':   pol.is_paid,
        })

    # Keep legacy fields for backward compat with existing payslip template rows
    leave_remaining_days = next(
        (b['remaining'] for b in leave_balances if 'annual' in b['label'].lower() or b['label'].lower().startswith('annual')),
        0.0
    )

    # Pass all numeric Decimal fields as floats so the template never touches Decimal
    rate = float(pr.hourly_rate_snapshot)
    pr_ctx = {
        'reference':            pr.reference,
        'period_start':         pr.period_start.isoformat(),
        'period_end':           pr.period_end.isoformat(),
        'period_year':          str(pr.period_end.year),
        'pay_date':             pr.pay_date.isoformat() if pr.pay_date else None,
        'status':               pr.status,
        'hourly_rate_snapshot': rate,
        'normal_hours':         float(pr.normal_hours),
        'overtime_hours':       float(pr.overtime_hours),
        'sunday_hours':         float(pr.sunday_hours),
        'holiday_hours':        float(pr.holiday_hours),
        'vacation_hours':       float(pr.vacation_hours),
        'sick_hours':           float(pr.sick_hours),
        'normal_pay':           float(pr.normal_pay),
        'overtime_pay':         float(pr.overtime_pay),
        'sunday_pay':           float(pr.sunday_pay),
        'holiday_pay':          float(pr.holiday_pay),
        'vacation_pay':         float(pr.vacation_pay),
        'gross_pay':            float(pr.gross_pay),
        'total_deductions':     float(pr.total_deductions),
        'net_pay':              float(pr.net_pay),
        'approved_at':          pr.approved_at.isoformat() if pr.approved_at else None,
        'created_at':           pr.created_at.isoformat() if pr.created_at else None,
        'rate_ot':              round(rate * 1.5, 2),
        'rate_sun':             round(rate * 2.0, 2),
        'rate_hol':             round(rate * 2.0, 2),
        'leave_hours_per_day':  hrs_per_day,
        'leave_entitlement_days': float(emp.leave_days_per_year),
    }

    return render_template('payslip.html',
        emp              = emp,
        pr               = pr_ctx,
        deductions       = json.loads(pr.deductions_json),
        employer_contribs = json.loads(pr.employer_contributions_json),
        advances         = json.loads(pr.advances_json),
        attendance       = json.loads(pr.attendance_json),
        store_name           = store_name,
        approved_by_name     = approved_by_name,
        ytd_gross            = ytd_gross,
        ytd_net              = ytd_net,
        ytd_vacation_days    = ytd_vacation_days,
        ytd_sick_days        = ytd_sick_days,
        leave_remaining_days = leave_remaining_days,
        leave_balances       = leave_balances,
    )


# ── Pay Rules ─────────────────────────────────────────────────────────────────

@bp.route('/api/employees/pay_rules', methods=['GET'])
def api_pay_rules_list():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    rows = PayRule.query.order_by(PayRule.sort_order).all()
    return jsonify([{
        'id': r.id, 'day_type': r.day_type, 'label': r.label,
        'multiplier': float(r.multiplier), 'is_paid': r.is_paid,
        'description': r.description,
    } for r in rows])


@bp.route('/api/employees/pay_rules/<int:rid>', methods=['PUT'])
def api_pay_rules_update(rid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    rule = PayRule.query.get_or_404(rid)
    d    = request.json or {}
    if 'multiplier' in d:  rule.multiplier = Decimal(str(d['multiplier']))
    if 'is_paid'    in d:  rule.is_paid     = bool(d['is_paid'])
    if 'description' in d: rule.description = d['description']
    db.session.commit()
    return jsonify({'ok': True})


# ── Password verification (teller self-service gate) ─────────────────────────

@bp.route('/api/employees/me/verify_password', methods=['POST'])
def api_verify_password():
    u = current_user()
    if not u:
        return jsonify({'error': 'Login required'}), 401
    from werkzeug.security import check_password_hash
    pw = (request.json or {}).get('password', '')
    ok = check_password_hash(u.password_hash, pw)
    return jsonify({'ok': ok})


# ── Pending leave requests (admin overview) ───────────────────────────────────

@bp.route('/api/employees/leaves/pending', methods=['GET'])
def api_leaves_pending():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    rows = (LeaveRequest.query
            .filter_by(status='requested')
            .order_by(LeaveRequest.created_at.desc())
            .all())
    result = []
    for r in rows:
        emp = Employee.query.get(r.employee_id)
        result.append({
            'id':            r.id,
            'employee_id':   r.employee_id,
            'employee_name': emp.name if emp else '?',
            'leave_type':    r.leave_type,
            'date_from':     r.date_from.isoformat(),
            'date_to':       r.date_to.isoformat(),
            'days_requested':float(r.days_requested),
            'reason':        r.reason,
            'created_at':    r.created_at.isoformat(),
        })
    return jsonify(result)


# ── Me (teller: find own employee record) ────────────────────────────────────

@bp.route('/api/employees/me', methods=['GET'])
def api_employees_me():
    u = current_user()
    if not u:
        return jsonify({'error': 'Login required'}), 401
    emp = Employee.query.filter_by(user_id=u.id, is_active=True).first()
    if not emp:
        return jsonify({'error': 'No employee record linked to your account'}), 404
    return jsonify(_serialize_employee(emp, include_sensitive=False))


# ── Public Holidays ───────────────────────────────────────────────────────────

@bp.route('/api/employees/public_holidays', methods=['GET'])
def api_public_holidays():
    if not require_login():
        return jsonify({'error': 'Login required'}), 401
    year = int(request.args.get('year', date.today().year))
    holidays = sa_public_holidays(year)
    return jsonify({d.isoformat(): name for d, name in holidays.items()})


# ── Dashboard ─────────────────────────────────────────────────────────────────

@bp.route('/api/employees/dashboard', methods=['GET'])
def api_employees_dashboard():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    employees = Employee.query.filter_by(is_active=True).all()
    emp_ids   = [e.id for e in employees]
    today     = date.today()
    month_start = today.replace(day=1)

    # Bulk query 1: hours worked this month for all employees
    att_rows = EmployeeAttendance.query.filter(
        EmployeeAttendance.employee_id.in_(emp_ids),
        EmployeeAttendance.work_date >= month_start,
        EmployeeAttendance.work_date <= today,
    ).with_entities(EmployeeAttendance.employee_id, EmployeeAttendance.hours_worked).all()
    hours_map = {}
    for eid, hrs in att_rows:
        hours_map[eid] = hours_map.get(eid, 0.0) + float(hrs or 0)

    # Bulk query 2: latest pay run per employee (one subquery)
    latest_pr_sub = (
        db.session.query(
            PayRun.employee_id,
            func.max(PayRun.period_end).label('max_end')
        ).filter(PayRun.employee_id.in_(emp_ids))
        .group_by(PayRun.employee_id)
        .subquery()
    )
    latest_runs = {
        row.employee_id: row
        for row in db.session.query(PayRun).join(
            latest_pr_sub,
            db.and_(
                PayRun.employee_id == latest_pr_sub.c.employee_id,
                PayRun.period_end  == latest_pr_sub.c.max_end,
            )
        ).all()
    }

    # Bulk query 3: outstanding advances per employee
    adv_rows = db.session.query(
        EmployeeAdvance.employee_id,
        func.sum(EmployeeAdvance.amount)
    ).filter(
        EmployeeAdvance.employee_id.in_(emp_ids),
        EmployeeAdvance.status == 'outstanding'
    ).group_by(EmployeeAdvance.employee_id).all()
    adv_map = {eid: float(total or 0) for eid, total in adv_rows}

    summaries = []
    for emp in employees:
        total_hrs    = hours_map.get(emp.id, 0.0)
        last_run     = latest_runs.get(emp.id)
        summaries.append({
            'id':               emp.id,
            'name':             emp.name,
            'hourly_rate':      float(emp.hourly_rate),
            'hours_this_month': round(total_hrs, 2),
            'gross_this_month': round(total_hrs * float(emp.hourly_rate), 2),
            'last_pay_run':     last_run.period_end.isoformat() if last_run else None,
            'last_pay_net':     float(last_run.net_pay) if last_run else None,
            'pending_advances': adv_map.get(emp.id, 0.0),
            'pay_frequency':    emp.pay_frequency,
            'pay_day_of_week':  emp.pay_day_of_week,
        })

    return jsonify({
        'employees':                summaries,
        'total_payroll_this_month': round(sum(s['gross_this_month'] for s in summaries), 2),
        'active_employee_count':    len(employees),
    })
