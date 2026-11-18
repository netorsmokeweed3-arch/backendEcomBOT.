from typing import Dict, List
from .base import BaseAdapter

class EpicentrAdapter(BaseAdapter):
    @property
    def raw_field_synonyms(self) -> Dict[str, List[str]]:
        # Створюємо копію базового словника та нові списки для уникнення мутації глобального стану
        synonyms = {k: list(v) for k, v in super().raw_field_synonyms.items()}
        synonyms["order_number"] = synonyms["order_number"] + ["number", "id"]
        synonyms["total_amount"] = synonyms["total_amount"] + ["totalAmount", "amount"]
        return synonyms