"""Traffic estimates, separate from measured compressed application payload."""
MONTH = 30 * 86400
RESERVE = 150 * 1024 * 1024


def project(budget, now):
    # A six-hour observation must not be divided by a full day.
    elapsed = max(60, now - budget['started_at'])
    total = budget['estimated_probe_bytes'] + budget['measured_upload_payload_bytes']
    return {'monthly_estimate_bytes': round(total / elapsed * MONTH + RESERVE),
            'observation_seconds': round(elapsed), 'provisional': elapsed < 86400,
            'accounting': 'payload_measured_plus_probe_estimates_and_keepalive_reserve'}
