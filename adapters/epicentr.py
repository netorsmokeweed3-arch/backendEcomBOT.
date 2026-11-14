from .base import BaseAdapter
from typing import Dict, List

class EpicentrAdapter(BaseAdapter):
    @property
    def raw_field_synonyms(self) -> Dict[str, List[str]]:
        synonyms = super().raw_field_synonyms
        synonyms["order_number"].extend(["number", "id"])
        synonyms["total_amount"].extend(["totalAmount", "amount"])
        return synonyms