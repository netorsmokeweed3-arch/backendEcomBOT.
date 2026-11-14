from .base import BaseAdapter
from typing import Dict, List

class RemonlineAdapter(BaseAdapter):
    @property
    def raw_field_synonyms(self) -> Dict[str, List[str]]:
        synonyms = super().raw_field_synonyms
        synonyms["order_number"].extend(["id_label", "id"])
        synonyms["total_amount"].extend(["price", "payed"])
        synonyms["customer_name"].extend(["client.name"])
        synonyms["phone"].extend(["client.phone"])
        return synonyms