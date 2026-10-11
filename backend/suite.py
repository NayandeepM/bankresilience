"""Measured suite reports and local-demo business probes; no model-generated actions."""
from __future__ import annotations
import time
import uuid
import threading
from html.parser import HTMLParser
from typing import Literal
import requests
from pydantic import Field
from control import Contract, latency_summary
from resilience import tolerance_verified


class SuiteSummary(Contract):
    headline: str
    findings: list[str] = Field(min_length=1, max_length=7)
    next_steps: list[str] = Field(min_length=1, max_length=3)


class PaymentOptions(HTMLParser):
    def __init__(self):
        super().__init__(); self.in_payments = False; self.accounts = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'select': self.in_payments = values.get('name') == 'account_num'
        if tag == 'option' and self.in_payments:
            account = values.get('value', '')
            if account.isdigit(): self.accounts.append(account)

    def handle_endtag(self, tag):
        if tag == 'select': self.in_payments = False


class PaymentBudget:
    def __init__(self, limit=15):
        self.limit=limit; self.attempted=0; self.lock=threading.Lock()

    def reserve(self):
        with self.lock:
            if self.attempted >= self.limit:return False
            self.attempted += 1
            return True


class DemoProbe:
    """Authenticated browser-equivalent requests using the configured local demo account.

    Tokens, credentials and account numbers never enter persisted evidence or AI input.
    A payment is dispatched once with a fresh UUID; a timeout is NEVER retried.
    """
    def __init__(self, frontend_container, verify_payments=False, budget=None, allowance=5):
        self.base = 'http://localhost:8082'
        self.session = requests.Session()
        # Each measurement opens a fresh frontend connection. Gunicorn may close an
        # idle keep-alive connection between baseline and injection; that must not
        # be mistaken for a fault in the banking dependency being tested.
        self.session.headers.update({'Connection':'close'})
        self.payments = verify_payments
        self.counts = {}
        self.budget = budget or PaymentBudget()
        self.baseline_payment_cap=1 if allowance >= 3 else 0
        self.recovery_payment_cap=1 if allowance >= 2 else 0
        self.fault_payment_cap=max(0,allowance-self.baseline_payment_cap-self.recovery_payment_cap)
        config = dict(e.split('=', 1) for e in frontend_container.attrs['Config']['Env'])
        response = self.session.post(self.base + '/login', data={
            'username': config['DEFAULT_USERNAME'], 'password': config['DEFAULT_PASSWORD']}, timeout=8)
        parser = PaymentOptions(); parser.feed(response.text)
        if response.status_code != 200 or not self.session.cookies or not parser.accounts:
            raise RuntimeError('Local demo login or payment-recipient preflight failed')
        self.recipient = parser.accounts[0]
        self.contact_count = len(parser.accounts)

    def check(self, phase):
        tick = time.monotonic()
        report = {'phase': phase, 'home': {'ok': False}, 'payment': {'attempted': False}}
        try:
            response = self.session.get(self.base + '/home', timeout=4)
            parser = PaymentOptions(); parser.feed(response.text)
            report['home'] = {'ok': response.status_code == 200 and len(parser.accounts) >= self.contact_count,
                             'status_code': response.status_code, 'contact_options': len(parser.accounts),
                             'latency_ms': round((time.monotonic() - tick) * 1000, 1), 'authenticated': bool(parser.accounts)}
        except requests.RequestException:
            report['home'] = {'ok': False, 'latency_ms': round((time.monotonic() - tick) * 1000, 1), 'error': 'Home request failed'}
        # Bound mutations to one before, up to three during, and one after each fault.
        cap = self.fault_payment_cap if phase == 'fault' else self.baseline_payment_cap if phase == 'baseline' else self.recovery_payment_cap
        if self.payments and self.counts.get(phase, 0) < cap and self.budget.reserve():
            self.counts[phase] = self.counts.get(phase, 0) + 1
            request_id = str(uuid.uuid4()); tick = time.monotonic()
            try:
                response = self.session.post(self.base + '/payment', data={
                    'account_num': self.recipient, 'amount': '0.01', 'uuid': request_id}, timeout=8)
                confirmed = response.status_code == 200 and 'Payment successful' in response.text
                report['payment'] = {'attempted': True, 'ok': confirmed, 'latency_ms': round((time.monotonic() - tick) * 1000, 1),
                                     'request_id': request_id, 'amount_cents': 1, 'retried': False,
                                     'outcome_unknown': not confirmed,
                                     'confirmation': 'Application success response' if confirmed else 'No success confirmation; do not retry automatically'}
            except requests.RequestException:
                report['payment'] = {'attempted': True, 'ok': False, 'request_id': request_id, 'retried': False,
                                     'outcome_unknown': True, 'latency_ms': round((time.monotonic() - tick) * 1000, 1)}
        return report

    def close(self):
        self.session.close()


def business_summary(points):
    homes = [p['home'] for p in points if p.get('home')]
    payments = [p['payment'] for p in points if p.get('payment', {}).get('attempted')]
    return {'home': latency_summary(homes), 'payments': latency_summary(payments),
            'payment_scope': 'Application success response; independent ledger audit required for accounting correctness',
            'ambiguous_payments': sum(bool(p.get('outcome_unknown')) for p in payments)}


def experiment_summary(result):
    target = result['target']
    points = result.get('fault_metrics', [])
    protection_points = result.get('tolerance_observations') or points
    protected = [p['resilience']['protected_probe'] for p in protection_points if p.get('resilience', {}).get('enabled')]
    tolerated = [p for p in protection_points if p.get('primary_fault_active', True) and tolerance_verified(p.get('resilience', {}))]
    observed_seconds = max(0, (tolerated[-1].get('fault_elapsed_seconds', 0) - tolerated[0].get('fault_elapsed_seconds', 0))) if tolerated else 0
    business = result.get('business_observations', [])
    during = [p for p in business if p['phase'] == 'fault']
    return {'experiment_id': result['id'], 'name': result.get('name', result['experiment_type']), 'target': target,
            'components':result.get('components',[]),'component_actions':result.get('component_actions',[]),
            'status': result['status'], 'baseline': result.get('baseline', {}).get(target),
            'fault': result.get('impact', {}).get('fault', {}).get(target),
            'recovery': result.get('impact', {}).get('recovery', {}).get(target),
            'detection_seconds': result.get('detection_time'), 'mitigation_seconds': result.get('time_to_mitigation'),
            'recovery_seconds': result.get('recovery_time'), 'recovery_verified': result.get('recovery_verified', False),
            'score': result.get('resilience_score', {}).get('value'),
            'diagnosis_source': (result.get('diagnosis') or {}).get('mode'), 'recovery_source': result.get('recovery_mode'),
            'protected_route': latency_summary(protected), 'tolerated_observations': len(tolerated),
            'fault_active_tolerance_seconds': round(observed_seconds, 3),
            'tolerance_verified': len(tolerated) >= 2 and observed_seconds >= 1,
            'business_during_fault': business_summary(during), 'business_all_phases': business_summary(business),
            'resilience_decision': result.get('resilience_decision'),
            'business_correctness': 'Only sampled local-demo flows measured; no claim about every banking operation'}


def summary_fallback(suite):
    rows = suite.get('results', [])
    recovered = sum(r['recovery_verified'] for r in rows)
    tolerated = sum(r['tolerance_verified'] for r in rows)
    return SuiteSummary(headline=f'{len(rows)} experiments recorded; {recovered} recovered; {tolerated} demonstrated protected routing during an active fault.',
        findings=[f"{r['name']}: recovery {'verified' if r['recovery_verified'] else 'unverified'}, "
                  f"protected availability {r['protected_route']['availability_percent']}%, "
                  f"{r['business_during_fault']['payments']['sample_count']} payment samples during the fault." for r in rows] or ['No fault completed.'],
        next_steps=['Review failed and ambiguous requests before repeating a write.',
                    'Shared database failures and simultaneous primary/standby failures are outside this suite.'])
