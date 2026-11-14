from .base import BaseAdapter
from typing import Dict, List

class KeyCrmAdapter(BaseAdapter):
    @property
    def raw_field_synonyms(self) -> Dict[str, List[str]]:
        synonyms = super().raw_field_synonyms
        synonyms["order_number"].extend(["source_uuid", "id"])
        synonyms["total_amount"].extend(["grand_total", "products_total"])
        synonyms["customer_name"].extend(["buyer.full_name"])
        synonyms["phone"].extend(["buyer.phone"])
        synonyms["email"].extend(["buyer.email"])
        return synonyms