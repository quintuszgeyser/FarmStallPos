"""Rev 5 P3-1b — proof that every AUDITED route in the `employees` blueprint
(employee records, attendance, schedule, leave, advances, loans, documents,
and payroll) writes an AuditLog row on completion."""
from datetime import date, timedelta
from decimal import Decimal

from werkzeug.security import generate_password_hash

from models import AuditLog, LeavePolicy, PayRule, db
from tests.factories import make_admin, make_employee
from tests.helpers import login_as


def _login_admin(client, username='employeesauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def test_employee_crud_writes_audit_events(db_session, client):
    _login_admin(client)

    resp = client.post('/api/employees', json={'name': 'Audit Employee'})
    assert resp.status_code == 201, resp.get_json()
    eid = resp.get_json()['id']
    assert _last('employee_created') is not None

    resp = client.put(f'/api/employees/{eid}', json={'hourly_rate': 75})
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_updated') is not None

    resp = client.delete(f'/api/employees/{eid}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_deactivated') is not None


def test_deduction_crud_writes_audit_events(db_session, client):
    emp = make_employee(name='Deduction Employee')
    _login_admin(client)

    resp = client.post(f'/api/employees/{emp.id}/deductions', json={'label': 'Union fee', 'amount': 50})
    assert resp.status_code == 201, resp.get_json()
    did = resp.get_json()['id']
    assert _last('employee_deduction_created') is not None

    resp = client.put(f'/api/employees/{emp.id}/deductions/{did}', json={'amount': 60})
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_deduction_updated') is not None

    resp = client.delete(f'/api/employees/{emp.id}/deductions/{did}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_deduction_deleted') is not None


def test_attendance_crud_writes_audit_events(db_session, client):
    emp = make_employee(name='Attendance Employee')
    _login_admin(client)
    work_date = date.today().isoformat()

    resp = client.post(f'/api/employees/{emp.id}/attendance', json={'date': work_date, 'hours': 8})
    assert resp.status_code == 201, resp.get_json()
    aid = resp.get_json()['id']
    assert _last('employee_attendance_created') is not None

    resp = client.post(f'/api/employees/{emp.id}/attendance', json={'date': work_date, 'hours': 9})
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_attendance_updated') is not None

    resp = client.delete(f'/api/employees/{emp.id}/attendance/{aid}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_attendance_deleted') is not None


def test_generate_and_clear_schedule_write_audit_events(db_session, client):
    make_employee(name='Schedule Employee')
    _login_admin(client)
    month = date.today().strftime('%Y-%m')

    resp = client.post('/api/employees/generate_schedule', json={'month': month})
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_schedule_generated') is not None

    resp = client.post('/api/employees/clear_schedule', json={'month': month})
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_schedule_cleared') is not None


def test_shift_schedule_crud_writes_audit_events(db_session, client):
    emp = make_employee(name='Shift Employee')
    _login_admin(client)
    sched_date = (date.today() + timedelta(days=7)).isoformat()

    resp = client.post(f'/api/employees/{emp.id}/schedule',
                        json={'date': sched_date, 'expected_start': '08:00', 'expected_end': '17:00'})
    assert resp.status_code == 201, resp.get_json()
    sid = resp.get_json()['id']
    assert _last('employee_shift_schedule_created') is not None

    resp = client.post(f'/api/employees/{emp.id}/schedule',
                        json={'date': sched_date, 'expected_start': '09:00', 'expected_end': '17:00'})
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_shift_schedule_updated') is not None

    resp = client.delete(f'/api/employees/{emp.id}/schedule/{sid}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_shift_schedule_deleted') is not None


def test_leave_request_approve_reject_cancel_write_audit_events(db_session, client):
    emp = make_employee(name='Leave Employee')
    if not LeavePolicy.query.filter_by(leave_type='annual').first():
        db.session.add(LeavePolicy(leave_type='annual', label='Annual', accrual_method='none', is_paid=True))
        db_session.flush()
    _login_admin(client)

    d_from = (date.today() + timedelta(days=10)).isoformat()
    d_to = (date.today() + timedelta(days=10)).isoformat()
    resp = client.post(f'/api/employees/{emp.id}/leaves', json={'date_from': d_from, 'date_to': d_to, 'leave_type': 'annual'})
    assert resp.status_code == 201, resp.get_json()
    lid = resp.get_json()['id']
    assert _last('employee_leave_requested') is not None

    resp = client.put(f'/api/employees/{emp.id}/leaves/{lid}/approve')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_leave_approved') is not None

    # New request, then reject
    resp = client.post(f'/api/employees/{emp.id}/leaves', json={'date_from': d_from, 'date_to': d_to, 'leave_type': 'annual'})
    lid2 = resp.get_json()['id']
    resp = client.put(f'/api/employees/{emp.id}/leaves/{lid2}/reject', json={'reason': 'Not enough cover'})
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_leave_rejected') is not None

    # New request, then cancel
    resp = client.post(f'/api/employees/{emp.id}/leaves', json={'date_from': d_from, 'date_to': d_to, 'leave_type': 'annual'})
    lid3 = resp.get_json()['id']
    resp = client.delete(f'/api/employees/{emp.id}/leaves/{lid3}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_leave_cancelled') is not None


def test_advance_create_and_cancel_write_audit_events(db_session, client):
    emp = make_employee(name='Advance Employee')
    _login_admin(client)

    resp = client.post(f'/api/employees/{emp.id}/advances', json={'amount': 500})
    assert resp.status_code == 201, resp.get_json()
    aid = resp.get_json()['id']
    assert _last('employee_advance_created') is not None

    resp = client.put(f'/api/employees/{emp.id}/advances/{aid}/cancel')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_advance_cancelled') is not None


def test_loan_create_writes_audit_event(db_session, client):
    emp = make_employee(name='Loan Employee')
    _login_admin(client)

    resp = client.post(f'/api/employees/{emp.id}/loans', json={'amount': 2000, 'installment': 200})
    assert resp.status_code == 201, resp.get_json()
    assert _last('employee_loan_created') is not None


def test_document_upload_and_delete_write_audit_events(db_session, client):
    import io
    emp = make_employee(name='Document Employee')
    _login_admin(client)

    resp = client.post(f'/api/employees/{emp.id}/documents',
                        data={'file': (io.BytesIO(b'fake pdf content'), 'contract.pdf'), 'document_type': 'contract'},
                        content_type='multipart/form-data')
    assert resp.status_code == 201, resp.get_json()
    did = resp.get_json()['id']
    assert _last('employee_document_uploaded') is not None

    resp = client.delete(f'/api/employees/{emp.id}/documents/{did}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_document_deleted') is not None


def _seed_pay_rules():
    for day_type, mult in (('normal', 1), ('overtime', Decimal('1.5')), ('sunday', 2), ('holiday', 2), ('vacation', 1), ('sick', 1)):
        if not PayRule.query.filter_by(day_type=day_type).first():
            db.session.add(PayRule(day_type=day_type, label=day_type.title(), multiplier=mult))
    db.session.flush()


def test_pay_run_lifecycle_writes_audit_events(db_session, client):
    emp = make_employee(name='PayRun Employee', hourly_rate=Decimal('60.00'))
    _seed_pay_rules()
    _login_admin(client)

    period_start = (date.today() - timedelta(days=14)).isoformat()
    period_end = date.today().isoformat()

    resp = client.post(f'/api/employees/{emp.id}/pay_runs', json={'period_start': period_start, 'period_end': period_end})
    assert resp.status_code == 201, resp.get_json()
    pid = resp.get_json()['id']
    assert _last('employee_pay_run_created') is not None

    resp = client.put(f'/api/employees/{emp.id}/pay_runs/{pid}/approve')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_pay_run_approved') is not None

    resp = client.put(f'/api/employees/{emp.id}/pay_runs/{pid}/paid')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_pay_run_marked_paid') is not None

    resp = client.put(f'/api/employees/{emp.id}/pay_runs/{pid}/revert_to_draft')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_pay_run_reverted_to_draft') is not None

    resp = client.delete(f'/api/employees/{emp.id}/pay_runs/{pid}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_pay_run_deleted') is not None


def test_pay_runs_bulk_writes_audit_event(db_session, client):
    make_employee(name='Bulk PayRun Employee', hourly_rate=Decimal('55.00'))
    _seed_pay_rules()
    _login_admin(client)

    period_start = (date.today() - timedelta(days=14)).isoformat()
    period_end = date.today().isoformat()
    resp = client.post('/api/employees/pay_runs/bulk', json={'period_start': period_start, 'period_end': period_end})
    assert resp.status_code == 201, resp.get_json()
    assert _last('employee_pay_runs_bulk_created') is not None


def test_schedule_rules_save_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/employees/schedule_rules', json={'mandatory_days': '5'})
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_schedule_rules_saved') is not None


def test_leave_policy_crud_writes_audit_events(db_session, client):
    _login_admin(client)

    resp = client.post('/api/employees/leave_policies', json={'leave_type': 'sabbatical', 'label': 'Sabbatical'})
    assert resp.status_code == 201, resp.get_json()
    assert _last('employee_leave_policy_created') is not None

    resp = client.put('/api/employees/leave_policies/sabbatical', json={'label': 'Sabbatical Leave'})
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_leave_policy_updated') is not None

    resp = client.delete('/api/employees/leave_policies/sabbatical')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_leave_policy_deleted') is not None


def test_leave_adjustment_crud_writes_audit_events(db_session, client):
    emp = make_employee(name='Adjustment Employee')
    _login_admin(client)

    resp = client.post(f'/api/employees/{emp.id}/leave_adjustments',
                        json={'adjustment_days': 2, 'leave_type': 'annual', 'reason': 'Goodwill bonus'})
    assert resp.status_code == 201, resp.get_json()
    aid = resp.get_json()['id']
    assert _last('employee_leave_adjustment_created') is not None

    resp = client.delete(f'/api/employees/{emp.id}/leave_adjustments/{aid}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_leave_adjustment_deleted') is not None


def test_pay_rule_update_writes_audit_event(db_session, client):
    _seed_pay_rules()
    rule = PayRule.query.filter_by(day_type='overtime').first()
    _login_admin(client)

    resp = client.put(f'/api/employees/pay_rules/{rule.id}', json={'multiplier': 1.75})
    assert resp.status_code == 200, resp.get_json()
    assert _last('employee_pay_rule_updated') is not None
