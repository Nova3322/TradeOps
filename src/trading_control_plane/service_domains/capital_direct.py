from __future__ import annotations

from trading_control_plane.service_domains.capital_direct_configuration import (
    DirectCapitalConfigurationService,
)
from trading_control_plane.service_domains.capital_direct_preview import DirectCapitalPreviewService
from trading_control_plane.service_domains.capital_direct_receipt import DirectCapitalReceiptService
from trading_control_plane.service_domains.capital_direct_submission import (
    DirectCapitalSubmissionService,
)


class DirectOperationCapitalService(
    DirectCapitalConfigurationService,
    DirectCapitalPreviewService,
    DirectCapitalSubmissionService,
    DirectCapitalReceiptService,
):
    """Stable direct-capital surface composed from lifecycle-owned state transitions."""


__all__ = ["DirectOperationCapitalService"]
