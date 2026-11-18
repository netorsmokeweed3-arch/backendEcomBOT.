from .base import BaseAdapter
from typing import Dict, List

class RemonlineAdapter(BaseAdapter):
    @property
    def raw_field_synonyms(self) -> Dict[str, List[str]]:
        synonyms = {k: list(v) for k, v in super().raw_field_synonyms.items()}
        synonyms["order_number"] = synonyms["order_number"] + ["id_label", "id"]
        synonyms["total_amount"] = synonyms["total_amount"] + ["price", "payed"]
        synonyms["customer_name"] = synonyms["customer_name"] + ["client.name"]
        synonyms["phone"] = synonyms["phone"] + ["client.phone"]
        return synonyms