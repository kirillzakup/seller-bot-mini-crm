"""Map a configured PaymentMethod to its provider implementation."""

from __future__ import annotations

from app.db.models.catalog import PaymentMethod
from app.db.models.enums import PaymentProviderType
from app.payments.base import PaymentProvider
from app.payments.cryptobot import CryptoBotProvider
from app.payments.epay import EpayProvider
from app.payments.freedompay import FreedomPayProvider
from app.payments.link import LinkPaymentProvider
from app.payments.manual import ManualTransferProvider
from app.payments.yookassa import YooKassaProvider

_PROVIDERS: dict[PaymentProviderType, type[PaymentProvider]] = {
    PaymentProviderType.MANUAL: ManualTransferProvider,
    PaymentProviderType.LINK: LinkPaymentProvider,
    PaymentProviderType.EPAY: EpayProvider,
    PaymentProviderType.FREEDOMPAY: FreedomPayProvider,
    PaymentProviderType.YOOKASSA: YooKassaProvider,
    PaymentProviderType.CRYPTOBOT: CryptoBotProvider,
}


def is_supported(provider_type: PaymentProviderType) -> bool:
    return provider_type in _PROVIDERS


def build_provider(method: PaymentMethod) -> PaymentProvider:
    cls = _PROVIDERS.get(method.type)
    if cls is None:
        raise NotImplementedError(f"Платёжный провайдер '{method.type}' ещё не реализован")
    return cls(config=method.config, instructions=method.instructions)
