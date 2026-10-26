"""The job lock keys: two jobs with the same key never run at the same time."""

import uuid


class LockKeys:
    @staticmethod
    def shop_etsy(shop_id: uuid.UUID) -> str:
        """One Etsy-facing maintenance job per shop at a time."""
        return f"shop:{shop_id}:etsy"

    @staticmethod
    def shop_orders(shop_id: uuid.UUID) -> str:
        """One order sync per shop at a time."""
        return f"shop:{shop_id}:orders"
