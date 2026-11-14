from abc import ABC
from typing import Dict, List, Tuple, Any, Optional
import logging

logger = logging.getLogger(__name__)

class BaseAdapter(ABC):
    # Використовуємо __slots__ для економії пам'яті та усунення зайвих __dict__ в інстансах
    __slots__ = ('_pre_split_synonyms', '_all_known_top_keys')

    # Оптимізація пам'яті: словник винесено на рівень класу (Class Attribute). 
    # Тепер він не дублюється в пам'яті при створенні кожного екземпляра адаптера.
    _DEFAULT_SYNONYMS: Dict[str, List[str]] = {
        "order_number": [
            "order_number", "orderNumber", "number", "name", "order_id", "id", 
            "order_no", "orderNo", "reference", "code", "invoice_no", "invoiceNumber",
            "order_reference", "external_id", "externalId", "display_number", "displayNumber",
            "order_code", "orderCode", "increment_id", "incrementId", "num", "order_num",
            "zakaz_id", "nomer_zakazu", "nomer", "transaction_id", "transactionId", "order_index",
            "order.id", "order.number", "order.name", "attributes.number", "data.id", "data.order_number"
        ],
        "created_at": [
            "created_at", "createdAt", "date_created", "date", "created", 
            "order_date", "date_added", "timestamp", "inserted_at", "processed_at",
            "processedAt", "date_modified", "updated_at", "updatedAt", "date_purchased",
            "order.created_at", "order.date", "attributes.created_at", "data.created_at"
        ],
        "customer_name": [
            "customer.full_name", "customer.fullName", "customer.name", "customer_name", "customerName",
            "client_name", "buyer_name", "full_name", "fullName", "buyer.full_name",
            "buyer.name", "client.name", "recipient_name", "shipping_address.name", "shippingAddress.name",
            "billing_address.name", "billingAddress.name", "shipping_name", "billing_name",
            "contact_name", "contactName", "receiver", "recipient", "pib", "fio"
        ],
        "first_name": [
            "billing.first_name", "shipping.first_name", "customer.first_name", "customer.firstName", 
            "buyer.first_name", "client.first_name", "shipping_address.first_name", "shippingAddress.first_name", 
            "billing_address.first_name", "billingAddress.first_name", "client_first_name", "first_name", 
            "firstName", "fname", "given_name", "givenName", "imya"
        ],
        "last_name": [
            "billing.last_name", "shipping.last_name", "customer.last_name", "customer.lastName", 
            "buyer.last_name", "client.last_name", "shipping_address.last_name", "shippingAddress.last_name", 
            "billing_address.last_name", "billingAddress.last_name", "client_last_name", "last_name", 
            "lastName", "lname", "surname", "family_name", "prizvyshche"
        ],
        "phone": [
            "billing.phone", "shipping.phone", "shipping_address.phone", "shippingAddress.phone", 
            "delivery_address.phone", "customer.phone", "buyer.phone", "client.phone", "phone",
            "billing_address.phone", "billingAddress.phone", "delivery.phone", "telephone", 
            "phone_number", "phoneNumber", "contact_phone", "client_phone", "mobile", "cellphone"
        ],
        "email": [
            "billing.email", "shipping.email", "email", "customer.email", "buyer.email", "client.email", 
            "shipping_address.email", "shippingAddress.email", "billing_address.email", "billingAddress.email", 
            "client_email", "customer_email", "mail", "contact_email", "contactEmail", "customer.mail", "e_mail"
        ],
        "street": [
            "billing.address_1", "shipping.address_1", "shipping_address.address1", "shippingAddress.address1", 
            "delivery_address.street", "deliveryAddress.street", "delivery.address", "address1", "address", "street", 
            "shipping_address.street", "billing_address.address1", "billingAddress.address1", "line1"
        ],
        "street2": [
            "billing.address_2", "shipping.address_2", "shipping_address.address2", "shippingAddress.address2", 
            "delivery_address.address2", "deliveryAddress.address2", "address2", "apartment", "apt", "suite", "unit", 
            "billing_address.address2", "billingAddress.address2", "line2", "building", "house"
        ],
        "city": [
            "billing.city", "shipping.city", "shipping_address.city", "shippingAddress.city", "delivery_address.city",
            "deliveryAddress.city", "delivery.city", "city", "billing_address.city", "billingAddress.city", 
            "town", "locality", "municipality", "settlement", "misto", "naselenyy_punkt", "gorod"
        ],
        "region": [
            "billing.state", "shipping.state", "shipping_address.province", "shipping_address.state", "delivery_address.region",
            "deliveryAddress.region", "province", "region", "state", "billing_address.province", 
            "billingAddress.province", "territory", "county", "oblast", "area"
        ],
        "postal_code": [
            "billing.postcode", "shipping.postcode", "shipping_address.zip", "shipping_address.postal_code", "delivery_address.postal_code",
            "deliveryAddress.postal_code", "zip", "postal_code", "postcode", "zip_code", 
            "zipCode", "billing_address.zip", "billingAddress.zip"
        ],
        "country": [
            "billing.country", "shipping.country", "shipping_address.country", "delivery_address.country", "country_name", 
            "country", "billing_address.country", "billingAddress.country", "country_code"
        ],
        "delivery_method": [
            "delivery_method", "shipping_lines.0.title", "delivery_option.name", "delivery.title",
            "shipping_method", "shippingMethod", "carrier", "shipping_title", "shipping_description",
            "shipping_lines", "shipping_line", "delivery_service", "deliveryService", "metod_dostavky"
        ],
        "currency": [
            "currency", "presentment_currency", "currency_code", "order_currency", "currencyCode",
            "total_price_set.shop_money.currency_code", "valyuta", "val", "iso_currency_code"
        ],
        "total_amount": [
            "total_price", "current_total_price", "total_amount", "totalAmount", "total",
            "total_price_set.shop_money.amount", "price", "amount",
            "grand_total", "order_total", "sum", "subtotal", "total_sum", "suma", "v_vsogo", "itog"
        ],
        "items": [
            "line_items", "items", "products", "goods", "order_items", "lineItems", "orderlines",
            "tovary", "poziciyi", "cart_items", "contents", "order_lines", "purchased_items"
        ],
        "item_title": [
            "title", "name", "product_name", "productName", "item_name", "label", "product_title",
            "nazva", "naimenuvannia", "description", "item_description", "nazvanie_tovara"
        ],
        "item_variant": [
            "variant_title", "variant", "size", "options", "variant_name", "attribute", "properties",
            "modifikatsiya", "opcija", "parametr", "item_options", "product_options", "variation"
        ],
        "item_sku": [
            "sku", "article", "code", "product_sku", "item_code", "productSku", "itemCode",
            "artikul", "art", "product_code", "productCode", "model", "vendor_code"
        ],
        "item_quantity": [
            "quantity", "qty", "count", "amount", "pieces", "k-st", "kilkist", "kol", "item_qty",
            "product_quantity", "item_quantity", "number_of_items", "item_count", "kolichestvo"
        ],
        "item_price": [
            "price", "unit_price", "cost", "item_price", "regular_price", "unitPrice", "price_set",
            "tsina", "vartist_odynytsi", "item_cost", "product_price", "sale_price", "base_price"
        ],
        "item_line_total": [
            "line_total", "lineTotal", "total", "final_line_price", "subtotal", "row_total",
            "suma_pozytsii", "total_price", "row_sum", "item_total", "itemTotal", "line_sum"
        ],
        "item_id": [
            "id", "line_item_id", "product_id", "item_id", "variant_id", "productId", "lineItemId"
        ],
        "status": [
            "status", "fulfillment_status", "financial_status", "status_name", "order_status", 
            "fulfillmentStatus", "financialStatus", "orderStatus", "stan", "stan_zamovlennya"
        ],
        "cancelled": [
            "cancelled_at", "is_cancelled", "canceled", "cancelled", "isCancelled", "cancel_reason",
            "skasovano", "is_canceled", "is_active", "order_cancelled", "is_deleted", "deleted"
        ],
    }

    @property
    def raw_field_synonyms(self) -> Dict[str, List[str]]:
        """Інтерфейс для дочірніх адаптерів (наприклад, Horoshop або KeyCRM можуть його доповнювати)."""
        return self._DEFAULT_SYNONYMS

    def __init__(self):
        # Кешуємо попередньо розбиті шляхи один раз при ініціалізації інстансу
        synonyms = self.raw_field_synonyms
        self._pre_split_synonyms: Dict[str, List[Tuple[str, ...]]] = {
            field: [tuple(path.split(".")) for path in paths]
            for field, paths in synonyms.items()
        }
        # Швидкий сэт для перевірки Schema Drift без повторних обчислень у рантаймі
        self._all_known_top_keys = {path[0] for paths in self._pre_split_synonyms.values() for path in paths}

    def get_field_paths(self, field: str) -> List[Tuple[str, ...]]:
        return self._pre_split_synonyms.get(field, [])

    @staticmethod
    def _get_nested_value(data: Any, path: Tuple[str, ...]) -> Any:
        """Ітеративний пошук за глибоким шляхом (захист від переповнення стеку / DoS)."""
        current = data
        for part in path:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    idx = int(part)
                    current = current[idx] if 0 <= idx < len(current) else None
                except ValueError:
                    return None
            else:
                return None
            if current is None:
                return None
        return current

    def extract_field(self, raw_data: dict, standard_field: str) -> Any:
        for path in self.get_field_paths(standard_field):
            val = self._get_nested_value(raw_data, path)
            if val is not None:
                return val
        return None

    def normalize_items(self, raw_data: dict) -> List[Dict[str, Any]]:
        """Повноцінна нормалізація товарних позицій (line items) усередині замовлення."""
        items_raw = self.extract_field(raw_data, "items")
        if not isinstance(items_raw, list):
            return []

        normalized_items = []
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            
            item_obj = {
                "id": self._get_nested_value(item, ("id",)) or self.extract_field(item, "item_id"),
                "title": self.extract_field(item, "item_title"),
                "variant": self.extract_field(item, "item_variant"),
                "sku": self.extract_field(item, "item_sku"),
                "quantity": self._get_nested_value(item, ("quantity",)) or self.extract_field(item, "item_quantity"),
                "price": self._get_nested_value(item, ("price",)) or self.extract_field(item, "item_price"),
                "line_total": self._get_nested_value(item, ("total",)) or self.extract_field(item, "item_line_total"),
            }
            normalized_items.append(item_obj)

        return normalized_items

    def normalize(self, raw_data: dict, platform_name: str) -> Tuple[Dict[str, Any], List[str]]:
        """Головний пайплайн нормалізації із захистом від невалідних типів та виявленням Schema Drift."""
        unknown_keys = []
        
        if not isinstance(raw_data, dict):
            logger.warning(f"Отримано невалідний тип сирого замовлення для платформи {platform_name}: {type(raw_data)}")
            return {}, []

        # Захист від AttributeError, якщо сторонній API повертає ключі не у вигляді стрічок (наприклад, числа)
        for raw_key in raw_data.keys():
            str_key = str(raw_key)
            if str_key not in self._all_known_top_keys and not str_key.startswith("_"):
                unknown_keys.append(str_key)

        normalized_order = {
            "platform": platform_name,
            "order_number": self.extract_field(raw_data, "order_number"),
            "created_at": self.extract_field(raw_data, "created_at"),
            "customer": {
                "name": self.extract_field(raw_data, "customer_name"),
                "first_name": self.extract_field(raw_data, "first_name"),
                "last_name": self.extract_field(raw_data, "last_name"),
                "phone": self.extract_field(raw_data, "phone"),
                "email": self.extract_field(raw_data, "email"),
            },
            "shipping": {
                "street": self.extract_field(raw_data, "street"),
                "street2": self.extract_field(raw_data, "street2"),
                "city": self.extract_field(raw_data, "city"),
                "region": self.extract_field(raw_data, "region"),
                "postal_code": self.extract_field(raw_data, "postal_code"),
                "country": self.extract_field(raw_data, "country"),
                "delivery_method": self.extract_field(raw_data, "delivery_method"),
            },
            "financials": {
                "currency": self.extract_field(raw_data, "currency"),
                "total_amount": self.extract_field(raw_data, "total_amount"),
            },
            "items": self.normalize_items(raw_data),
            "status": self.extract_field(raw_data, "status"),
            "is_cancelled": bool(self.extract_field(raw_data, "cancelled")),
            "raw_payload": raw_data
        }

        return normalized_order, unknown_keys



    