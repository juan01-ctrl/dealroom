CREATE INDEX mail_attempts_pending_age ON mail_attempts (created_at)
    WHERE state = 'PENDING_CONFIRMATION';
