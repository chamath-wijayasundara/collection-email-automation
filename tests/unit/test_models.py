from datetime import date
from decimal import Decimal

from collection_email.domain.models import ARRecord


def test_ar_record_preserves_money_and_both_aging_values():
    record = ARRecord(
        source_row=2,
        customer_name="Example Ltd",
        customer_email="ar@example.test",
        document_number="INV-1",
        amount=Decimal("100.50"),
        issue_date=date(2026, 1, 1),
        due_date=date(2026, 3, 21),
        source_aging_days=4,
        effective_aging_days=4,
        aging_bucket="0-30",
    )

    assert record.amount == Decimal("100.50")
    assert record.effective_aging_days == 4
