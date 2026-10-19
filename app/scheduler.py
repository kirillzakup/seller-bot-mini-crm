"""APScheduler setup — runs the background jobs on intervals."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.services.jobs import (
    auto_analyze_script,
    expire_subscriptions,
    greet_birthdays,
    nudge_abandoned_orders,
    nudge_silent_leads,
    reconcile_pending_payments,
    refresh_instagram_token,
    remind_leads_in_work,
    remind_waiting_leads,
    send_due_followups,
)

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(expire_subscriptions, "interval", minutes=30, id="expire_subscriptions")
    _scheduler.add_job(nudge_abandoned_orders, "interval", minutes=30, id="nudge_abandoned")
    _scheduler.add_job(reconcile_pending_payments, "interval", minutes=10, id="reconcile_payments")
    _scheduler.add_job(send_due_followups, "interval", minutes=10, id="send_followups")
    _scheduler.add_job(nudge_silent_leads, "interval", minutes=15, id="nudge_silent")
    _scheduler.add_job(greet_birthdays, "interval", hours=6, id="greet_birthdays")
    _scheduler.add_job(refresh_instagram_token, "interval", hours=12, id="refresh_ig_token")
    _scheduler.add_job(auto_analyze_script, "interval", hours=24, id="auto_analyze_script")
    # Каждые 15 минут, а срабатывает раз в сутки в назначенный час — см. job.
    _scheduler.add_job(remind_leads_in_work, "interval", minutes=15, id="remind_leads")
    # Заявки «В ожидании»: в назначенный менеджером час карточка снова в теме заявок.
    _scheduler.add_job(remind_waiting_leads, "interval", minutes=5, id="remind_waiting_leads")
    _scheduler.start()
    logger.info(
        "Scheduler started: expire/nudge 30m, reconcile 10m, followups 10m, "
        "silent 15m, bday 6h, ig-token 12h, auto-analyze 24h, viz-purge 1h, "
        "заявки в работе — раз в сутки"
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
