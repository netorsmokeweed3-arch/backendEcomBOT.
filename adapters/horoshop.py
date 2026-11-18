from .base import BaseAdapter
from typing import Dict, List

class HoroshopAdapter(BaseAdapter):
    @property
    def raw_field_synonyms(self) -> Dict[str, List[str]]:
        synonyms = {k: list(v) for k, v in super().raw_field_synonyms.items()}
        synonyms["order_number"] = synonyms["order_number"] + ["order_id", "id"]
        synonyms["total_amount"] = synonyms["total_amount"] + ["total_sum", "sum_to_pay"]
        synonyms["delivery_method"] = synonyms["delivery_method"] + ["delivery_type", "delivery_title"]
        return synonyms