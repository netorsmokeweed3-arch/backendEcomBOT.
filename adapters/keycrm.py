from .base import BaseAdapter
from typing import Dict, List

class KeyCrmAdapter(BaseAdapter):
    @property
    def raw_field_synonyms(self) -> Dict[str, List[str]]:
        synonyms = {k: list(v) for k, v in super().raw_field_synonyms.items()}
        synonyms["order_number"] = synonyms["order_number"] + ["source_uuid", "id"]
        synonyms["total_amount"] = synonyms["total_amount"] + ["grand_total", "products_total"]
        synonyms["customer_name"] = synonyms["customer_name"] + ["buyer.full_name"]
        synonyms["phone"] = synonyms["phone"] + ["buyer.phone"]
        synonyms["email"] = synonyms["email"] + ["buyer.email"]
        return synonyms