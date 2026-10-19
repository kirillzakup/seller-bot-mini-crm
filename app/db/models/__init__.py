"""Import every model so it registers on ``Base.metadata`` for create_all."""

from app.db.models.analytics import ChatReport
from app.db.models.catalog import PaymentMethod, Product, ProductCategory, ProductMedia
from app.db.models.content import (
    CommentFunnel,
    Creative,
    Document,
    Event,
    Followup,
    KbItem,
    KbSuggestion,
    MediaAsset,
    Message,
    ScriptProposal,
    Setting,
)
from app.db.models.enums import (
    Channel,
    ConversationRole,
    FulfillmentType,
    KbKind,
    LeadStatus,
    OrderStatus,
    PaymentProviderType,
    PaymentStatus,
    TicketStatus,
)
from app.db.models.lead import Lead
from app.db.models.order import AccessGrant, Order, Payment
from app.db.models.user import User

__all__ = [
    "ChatReport",
    "User",
    "Lead",
    "Product",
    "ProductCategory",
    "ProductMedia",
    "PaymentMethod",
    "Order",
    "Payment",
    "AccessGrant",
    "Setting",
    "Event",
    "Followup",
    "Message",
    "KbItem",
    "KbSuggestion",
    "ScriptProposal",
    "MediaAsset",
    "CommentFunnel",
    "Creative",
    "Document",
    # enums
    "Channel",
    "OrderStatus",
    "PaymentStatus",
    "PaymentProviderType",
    "FulfillmentType",
    "KbKind",
    "LeadStatus",
    "TicketStatus",
    "ConversationRole",
]
