from .base import BaseAdapter
from typing import Dict, List

class HoroshopAdapter(BaseAdapter):
    @property
    def raw_field_synonyms(self) -> Dict[str, List[str]]:
        synonyms = super().raw_field_synonyms
        synonyms["order_number"].extend(["order_id", "id"])
        synonyms["total_amount"].extend(["total_sum", "sum_to_pay"])
        synonyms["delivery_method"].extend(["delivery_type", "delivery_title"])
        return synonyms