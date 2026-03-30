# Email marketing (runbook)

## Celery Beat

`CEBackend/settings.py` registers:

- `dispatch-due-scheduled-marketing-campaigns` — every minute; picks `BusinessEmailCampaign` rows with `status=scheduled` and `scheduled_at <= now`, sets `sending`, enqueues `send_business_marketing_campaign_task`.
- `process-due-marketing-enrollments` — every minute; runs `process_due_workflow_enrollments` for active workflow enrollments whose `next_run_at` has passed.

Beat and workers must be running in each environment where scheduling or automations are required.

## Env / Stripe

- Email marketing tiers: `EMAIL_MARKETING_*_PRICE_ID` in Django settings (`email_marketing_config.py`).
- Resend: `RESEND_API_KEY`, public API base for unsubscribe (`DJANGO_PUBLIC_URL`).

## Quota and sends

- Marketing quota is enforced in `MarketingCampaignSendView`, scheduled send (`MarketingCampaignScheduleView`), and again at the start of `send_business_marketing_campaign_task`.
- Successful batch sends call `increment_marketing_sent` once per completed task.

## Tier changes mid-flight

- Scheduled campaigns: recipient count and quota are checked when the user schedules and again when the send task runs; a downgrade can cause the task to mark the campaign `failed` with a quota message.
- Active workflows: step runner re-checks addon and `automation_enabled`; missing addon cancels the enrollment.

## Limitations (v1)

- Workflow builder in the dashboard creates a linear delay + email sequence; advanced branching is not implemented.
- `booking_completed` automation fires when `Booking.status` is saved as `completed` (see `signals.py`).
