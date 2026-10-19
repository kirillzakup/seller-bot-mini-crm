"""Admin router — mounts all admin sub-routers under /admin."""

from __future__ import annotations

from fastapi import APIRouter

from app.admin.routes import (
    auth,
    broadcast,
    channels,
    clients,
    creatives,
    dashboard,
    dialogs,
    documents,
    funnels,
    guide,
    kb,
    leads,
    learning,
    media,
    orders,
    payment_methods,
    products,
    push,
    reports,
    settings,
)

router = APIRouter(prefix="/admin")
router.include_router(auth.router)
# Dashboard lives at exactly /admin (empty path on the prefixed router)
router.add_api_route("", dashboard.dashboard, methods=["GET"], name="dashboard")
router.include_router(products.router)
router.include_router(payment_methods.router)
router.include_router(orders.router)
router.include_router(dialogs.router)
router.include_router(leads.router)  # /admin/leads (заявки и продажи)
router.include_router(push.router)
router.include_router(clients.router)
router.include_router(broadcast.router)
router.include_router(kb.router)
router.include_router(learning.router)
router.include_router(media.router)
router.include_router(documents.router)
router.include_router(funnels.router)
router.include_router(creatives.router)
router.include_router(channels.router)
router.include_router(reports.router)  # /admin/reports (аналитика чатов, выгрузки)
router.include_router(settings.router)
router.include_router(guide.router)  # /admin/setup (wizard) + /admin/help
