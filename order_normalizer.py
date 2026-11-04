"""Універсальний обробник замовлень для будь-якого магазину.
Підтримує мульти-магазинний паралельний збір даних (Shopify, Prom, Rozetka тощо).
"""
import re
import asyncio
import logging
from functools import lru_cache
from typing import Any, Optional, List, Dict
import httpx

logger = logging.getLogger(__name__)

# Використовуємо звичайний кортеж замість множини, щоб уникнути будь-яких помилок хешування
EMPTY = (None, "", (), {}, "None", "null", "N/A", "n/a")

# Базові префікси для форматування валют
CURRENCY_PREFIX = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "UAH": "₴"
}

FIELD_SYNONYMS = {
    "order_number": [
        "order_number", "orderNumber", "number", "name", "order_id", "id", 
        "order_no", "orderNo", "reference", "code", "invoice_no", "invoiceNumber",
        "order_reference", "external_id", "externalId", "display_number", "displayNumber",
        "order_code", "orderCode", "increment_id", "incrementId", "num", "order_num",
        "zakaz_id", "nomer_zakazu", "nomer", "transaction_id", "transactionId", "order_index",
        "order_identifier", "orderIdentifier", "doc_number", "document_number", "sales_order_id",
        "order_num_id", "order_key", "orderKey", "order_uid", "orderUID", "check_number",
        "checkNumber", "receipt_number", "receiptNumber", "order_identifier_str", "code_order",
        "identifikator_zamovlennya", "nomer_zakaza", "zakaz_nomer", "id_zakaza", "shop_order_id",
        "order_ref", "ord_num", "ord_id", "nr_zamowienia", "bestellnummer", "numro_de_commande",
        "order.id", "order.number", "order.name", "attributes.number", "data.id", "data.order_number",
        "properties.order_id", "meta.order_id", "order_properties.id", "order_details.number",
        "unique_id", "uniqueId", "guid", "GUID", "order_guid", "orderGUID", "se_order_id"
    ],
    "created_at": [
        "created_at", "createdAt", "date_created", "date", "created", 
        "order_date", "date_added", "timestamp", "inserted_at", "processed_at",
        "processedAt", "date_modified", "updated_at", "updatedAt", "date_purchased",
        "created_time", "createdTime", "order_time", "orderTime", "datetime",
        "date_added_to_db", "time", "order_timestamp", "orderTimestamp", "date_placed",
        "creation_date", "creationDate", "placed_at", "placedAt", "order_date_added",
        "date_created_gmt", "dateCreatedGmt", "purchase_date", "purchaseDate",
        "order_datetime", "orderDatetime", "time_created", "timeCreated", "data_stvoreння",
        "data_zamovlennya", "дата_создания", "дата_заказа", "data_sozdaniya", "data_zakaza",
        "order_created_at", "created_date", "createdDate", "data_utvorenya", "data_oformlennya",
        "order.created_at", "order.date", "attributes.created_at", "data.created_at",
        "meta.created_at", "order_info.created_at", "date_order", "order_date_gmt",
        "epoch_time", "unix_time", "unixTime", "created_timestamp", "createdTimestamp"
    ],
    "customer_name": [
        "customer.full_name", "customer.fullName", "customer.name", "customer_name", "customerName",
        "client_name", "buyer_name", "full_name", "fullName", "buyer.full_name",
        "buyer.name", "client.name", "recipient_name", "shipping_address.name", "shippingAddress.name",
        "billing_address.name", "billingAddress.name", "shipping_name", "billing_name",
        "contact_name", "contactName", "receiver", "recipient", "customer_fullname",
        "client_fullname", "buyer_fullname", "pib", "fio", "name_surname", "client_fio",
        "customer_fio", "buyer_fio", "imya_prizvyshche", "osoba", "kontragent",
        "customer.display_name", "buyer.display_name", "customer.username", "customerNameSurname",
        "client_full_name", "buyer_full_name", "customer_details.name", "user_name", "userName",
        "customer.first_name_last_name", "customer.name_full", "client_full_name_str",
        "buyer_f_i_o", "p_i_b", "pip", "prizvyshche_imya", "polnoe_imya", "familiya_imya",
        "shipping_address.fullname", "shipping_address.full_name", "billing_address.full_name",
        "customer.customer_name", "buyer.customer_name", "client_details.name", "order_customer_name",
        "customer_info.name", "buyer_info.name", "client_info.name", "customer_profile.name",
        "customer_data.full_name", "buyer_data.full_name", "recipient_full_name", "shipping_fullname"
    ],
    "first_name": [
        "customer.first_name", "customer.firstName", "buyer.first_name", "client.first_name",
        "shipping_address.first_name", "shippingAddress.first_name", "billing_address.first_name", "billingAddress.first_name",
        "client_first_name", "first_name", "firstName", "shipping_firstname", "billing_firstname",
        "fname", "given_name", "givenName", "imya", "customer_firstname", "buyer_firstname",
        "customer.fname", "buyer.fname", "shipping_address.fname", "billing_address.fname", "client_fname",
        "user_firstname", "userFirstname", "name_first", "customer_imya", "imya_pokupcya",
        "shipping_address.given_name", "billing_address.given_name", "client_given_name",
        "imie", "vorname", "prenom", "customer_first_name",
        "customer_info.first_name", "buyer_info.first_name", "client_info.first_name", "customer.given_name"
    ],
    "last_name": [
        "customer.last_name", "customer.lastName", "buyer.last_name", "client.last_name",
        "shipping_address.last_name", "shippingAddress.last_name", "billing_address.last_name", "billingAddress.last_name",
        "client_last_name", "last_name", "lastName", "shipping_lastname", "billing_lastname",
        "lname", "surname", "family_name", "familyName", "prizvyshche", "customer_lastname", "buyer_lastname",
        "customer.lname", "buyer.lname", "shipping_address.lname", "billing_address.lname", "client_lname",
        "user_lastname", "userLastname", "name_last", "familiya", "prizvyshche_pokupcya",
        "shipping_address.family_name", "billing_address.family_name", "client_family_name",
        "nazwisko", "nachname", "nom", "customer_last_name",
        "customer_info.last_name", "buyer_info.last_name", "client_info.last_name", "customer.family_name"
    ],
    "phone": [
        "shipping_address.phone", "shippingAddress.phone", "delivery_address.phone",
        "deliveryAddress.phone", "customer.phone", "buyer.phone", "client.phone", "phone",
        "billing_address.phone", "billingAddress.phone", "delivery.phone", "telephone", 
        "phone_number", "phoneNumber", "contact_phone", "client_phone", "mobile", "cellphone",
        "customer_phone", "contact_number", "contactNumber", "tel", "cell", "customer.phone_number",
        "shipping_phone", "billing_phone", "telefon", "nomer_telefonu", "mobilnyi", "mob_phone",
        "customer.telephone", "buyer.phone_number", "recipient_phone", "customer_mobile",
        "billing_address.telephone", "shipping_address.telephone", "contact_info.phone",
        "customer_tel", "buyer_tel", "client_tel", "mobitel", "phone_no", "phoneNo",
        "contact_details.phone", "user_phone", "userPhone", "phone_contact", "tel_number",
        "nomer_mob", "mobilny_nomer", "kontakt_telefon", "contact_telephone",
        "telefon_kontaktowy", "telefonnummer", "numro_de_telephone", "phone_num", "cell_phone",
        "customer.phone_no", "buyer.phone_no", "shipping_address.telephone_number",
        "order_customer.phone", "client_data.phone", "customer_profile.phone", "contact.phone"
    ],
    "email": [
        "email", "customer.email", "buyer.email", "client.email", "shipping_address.email", 
        "shippingAddress.email", "billing_address.email", "billingAddress.email", "client_email", 
        "customer_email", "mail", "contact_email", "contactEmail", "customer.mail", "e_mail",
        "elektronna_poshta", "pochta", "buyer.email_address", "customer.email_address",
        "shipping_address.customer_email", "user_email", "userEmail", "contact_info.email",
        "customer_mail", "buyer_mail", "client_mail", "e-mail", "electronic_mail",
        "elektronka", "el_pochta", "poshta", "mail_address", "mailAddress", "email_address",
        "emailAddress", "contact_mail", "customer_e_mail", "adresa_e_mail", "email_addr",
        "customer.email_address", "buyer.email_address", "client_data.email", "order_email"
    ],
    "street": [
        "shipping_address.address1", "shippingAddress.address1", "delivery_address.street",
        "deliveryAddress.street", "delivery_address.address1", "deliveryAddress.address1",
        "delivery.address", "address1", "address", "street", "shipping_address.street", 
        "billing_address.address1", "billingAddress.address1", "line1", "street_address", "streetAddress",
        "shipping_address.address", "shippingAddress.address", "delivery_address.address", "deliveryAddress.address",
        "street_name", "streetName", "delivery_street", "vulytsia", "vul", "street1",
        "shipping_address.street_address", "delivery_address.line1", "address_1", "addressLine1",
        "shipping_address.address_1", "billing_address.address_1", "street1_address",
        "shipping_street", "billing_street", "delivery_address1", "delivery_street_address",
        "vulicyia", "ulica", "adress", "street_line1", "streetLine1", "location_address",
        "ul", "str", "strasse", "rue", "adres_ulica", "street1_line",
        "shipping_address.street_line1", "delivery_address.address_line1", "shipping.address1"
    ],
    "street2": [
        "shipping_address.address2", "shippingAddress.address2", "delivery_address.address2",
        "deliveryAddress.address2", "address2", "apartment", "apt", "suite", "unit", 
        "billing_address.address2", "billingAddress.address2", "line2", "building", "house", "room",
        "apartment_number", "apartmentNumber", "flat", "kv", "bud", "korпус", "building_number",
        "dom", "kvartira", "ofis", "office", "shipping_address.apartment", "delivery_address.apartment",
        "address_2", "addressLine2", "shipping_address.address_2", "billing_address.address_2",
        "house_number", "houseNumber", "building_no", "flat_no",
        "building_num", "room_number", "roomNumber", "office_number", "officeNumber",
        "podyzd", "podyizd", "etazh", "floor", "dop_address", "additional_address",
        "shipping_address.suite", "billing_address.suite", "shipping_address.unit", "billing_address.unit",
        "nr_domu", "nr_mieszkania", "bâtiment", "etage", "shipping_address.address_2"
    ],
    "city": [
        "shipping_address.city", "shippingAddress.city", "delivery_address.city",
        "deliveryAddress.city", "delivery.city", "city", "billing_address.city", "billingAddress.city", 
        "town", "locality", "municipality", "settlement", "misto", "naselenyy_punkt", "gorod",
        "shipping_address.town", "delivery_address.city_name", "billing_address.town", "city_name",
        "delivery_city", "shipping_city", "billing_city", "naseleniy_punkt", "miejsce",
        "town_city", "townCity", "city_town", "cityTown", "lokalizacja",
        "ort", "ville", "misto_naselenyy_punkt", "delivery_town", "shipping_address.city_name"
    ],
    "region": [
        "shipping_address.province", "shippingAddress.province", "shipping_address.state",
        "shippingAddress.state", "delivery_address.region", "deliveryAddress.region",
        "province", "region", "state", "billing_address.province", "billingAddress.province", 
        "billing_address.state", "billingAddress.state", "territory", "county", "oblast", "area",
        "shtat", "reys", "rayon", "shipping_address.region_name", "state_province", "stateProvince",
        "delivery_region", "shipping_region", "billing_region", "oblast_name", "region_name",
        "administrativnyy_rayon", "subdivision", "subDivision",
        "wojewodztwo", "bundesland", "region_state", "shipping_address.province_code"
    ],
    "postal_code": [
        "shipping_address.zip", "shippingAddress.zip", "shipping_address.postal_code",
        "shippingAddress.postal_code", "delivery_address.postal_code", "deliveryAddress.postal_code",
        "zip", "postal_code", "postcode", "zip_code", "zipCode", "billing_address.zip", "billingAddress.zip",
        "index", "post_index", "poshtovyi_indeks", "shipping_address.postcode", "billing_address.postal_code",
        "delivery_postal_code", "shipping_postal_code", "billing_postal_code", "indeks",
        "pochtovyy_indeks", "postalCodeNum", "zip_codes", "postal", "postalCodeString",
        "kod_pocztowy", "plz", "code_postal", "postal_index", "shipping_address.zip_code"
    ],
    "country": [
        "shipping_address.country", "shippingAddress.country", "shipping_address.country_name",
        "shippingAddress.country_name", "delivery_address.country", "deliveryAddress.country",
        "country_name", "country", "billing_address.country", "billingAddress.country", "country_code", "countryCode",
        "krayina", "strana", "shipping_address.country_code", "billing_address.country_code",
        "delivery_country", "shipping_country", "billing_country", "derzhava", "land", "pais",
        "kraj", "paays", "country_iso", "shipping_address.country_iso"
    ],
    "delivery_method": [
        "delivery_method", "shipping_lines.0.title", "delivery_option.name", "delivery.title",
        "shipping_method", "shippingMethod", "carrier", "shipping_title", "shipping_description",
        "shipping_lines", "shipping_line", "delivery_service", "deliveryService", "metod_dostavky",
        "sposib_dostavky", "transport", "shipping_carrier", "shipping_method_title", "shipping_type",
        "delivery_type", "deliveryType", "shipping_type_title", "shipping_provider", "shippingProvider",
        "sposob_dostavki", "metod_dos", "dostavka", "shipping_title_str", "courier", "logistic_service",
        "sposob_oplaty_i_dostavki", "shipping_option", "nova_poshta", "ukrposhta", "shipping.title"
    ],
    "currency": [
        "currency", "presentment_currency", "currency_code", "order_currency", "currencyCode",
        "total_price_set.shop_money.currency_code", "total_price_set.presentment_money.currency_code",
        "shop_money.currency_code", "valyuta", "val", "iso_currency_code", "order_currency_code",
        "currency_symbol", "currencySymbol", "valuta", "v_valyuti", "money_currency",
        "shop_currency", "base_currency", "transaction_currency",
        "curr_code", "waluta", "wahrung", "devise", "order.currency"
    ],
    "total_amount": [
        "total_price", "current_total_price", "total_amount", "totalAmount", "total",
        "total_price_set.shop_money.amount", "total_price_set.presentment_money.amount", "price", "amount",
        "grand_total", "order_total", "sum", "subtotal", "total_sum", "totalSum", "total_price_usd",
        "suma", "v_vsogo", "itog", "total_price_set.shop_money.shop_amount", "final_amount",
        "total_paid", "order_amount", "summa", "total_price_decimal",
        "total_cost", "totalCost", "order_sum", "orderSum", "itogo", "vsego", "k_oplate",
        "summa_zakaza", "suma_zamovlennya", "full_price", "fullPrice", "total_due",
        "balance_due", "net_total", "gross_total", "final_price", "finalPrice",
        "kwota", "gesamtbetrag", "montant_total", "total_price_num", "order.total_price"
    ],
    "items": [
        "line_items", "items", "products", "goods", "order_items", "lineItems", "orderlines", "products_list",
        "tovary", "poziciyi", "cart_items", "contents", "order_lines", "purchased_items",
        "product_list", "order_products", "elements", "positions", "zakaz_tovary",
        "zmist", "detali_zamovlennya", "order_contents", "cart_contents", "products_array",
        "produkty", "pozycje", "articles", "order_details", "order.line_items", "data.items"
    ],
    "item_title": [
        "title", "name", "product_name", "productName", "item_name", "label", "product_title",
        "nazva", "naimenuvannia", "product_label", "item_label", "product_nicename",
        "tovar_nazva", "product_heading", "item_title_str", "description", "item_description",
        "nazvanie_tovara", "naimenovanie", "product_caption",
        "produkt_name", "nazwa_produktu", "artikel_name", "item.title", "product.name"
    ],
    "item_variant": [
        "variant_title", "variant", "size", "options", "variant_name", "attribute", "properties", "variantTitle",
        "modifikatsiya", "opcija", "parametr", "item_options", "product_options", "variation",
        "product_variant", "item_variant_name", "options_text", "attr", "atribut",
        "property", "item_attributes", "product_attributes", "kolir_rozmir", "size_color",
        "rozmir", "kolir", "color", "option1", "option2", "option3", "variant.title"
    ],
    "item_sku": [
        "sku", "article", "code", "product_sku", "item_code", "productSku", "itemCode",
        "artikul", "art", "product_code", "productCode",
        "model", "item_sku_code", "product_model", "vendor_code", "vendorCode",
        "articul", "artikyl", "kod_tovara", "product_artikul",
        "sygnatura", "artikelnummer", "ref_article", "item.sku"
    ],
    "item_quantity": [
        "quantity", "qty", "count", "amount", "pieces", "k-st", "kilkist", "kol", "item_qty",
        "product_quantity", "item_quantity",
        "number_of_items", "item_count", "itemCount", "kol_vo", "kolichestvo",
        "odynytsi", "shтук", "pcs", "item_pieces", "quantity_ordered",
        "sztuk", "menge", "quantite", "item.quantity"
    ],
    "item_price": [
        "price", "unit_price", "cost", "item_price", "regular_price", "unitPrice", "price_set",
        "tsina", "vartist_odynytsi", "item_cost", "product_price",
        "product_cost", "productCost", "price_per_item", "tovarnaya_tsena", "tsena_za_shtuku",
        "unit_cost", "unitCost", "sale_price", "salePrice", "base_price", "basePrice",
        "cena", "preis", "prix_unitaire", "item.price"
    ],
    "item_line_total": [
        "line_total", "lineTotal", "total", "final_line_price", "subtotal", "row_total", "total_discount",
        "suma_pozytsii", "total_price", "row_sum", "item_total",
        "itemTotal", "line_sum", "lineSum", "total_row", "sum_row", "row_price",
        "itogo_po_pozitsii", "suma_ryadka", "total_item_price",
        "wartosc", "gesamtpreis", "item.total"
    ],
    "item_id": [
        "id", "line_item_id", "product_id", "item_id", "variant_id", "productId", "lineItemId",
        "item_identifier",
        "product_id_in_store", "item_uid", "goods_id", "tovar_id", "variant_id_str", "item.id"
    ],
    "status": [
        "status", "fulfillment_status", "financial_status", "status_name", "order_status", 
        "fulfillmentStatus", "financialStatus", "orderStatus", "stan", "stan_zamovlennya",
        "order_state", "state", "payment_status", "paymentStatus",
        "current_status", "currentStatus", "status_zakazu", "sostoyanie", "order_status_title",
        "stage", "process_status", "status_code", "statusCode",
        "status_zamowienia", "bestellstatus", "order.status"
    ],
    "cancelled": [
        "cancelled_at", "is_cancelled", "canceled", "cancelled", "isCancelled", "cancel_reason",
        "skasovano", "is_canceled", "is_active", "order_cancelled", "cancelled_status",
        "is_deleted", "deleted", "status_cancel", "otmenen", "anulovano", "cancel_status",
        "is_void", "voided", "is_voided", "reason_cancelled", "canceled_at",
        "anulowany", "storniert", "order.cancelled"
    ],
}


class OrderAPIError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def normalize_shop(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    value = re.sub(r"^https?://", "", raw.strip().lower()).split("/")[0]
    if not value:
        return None
    if "." not in value:
        value = f"{value}.myshopify.com"
    return value


@lru_cache(maxsize=128)
def _cached_split_path(path: Any) -> tuple:
    """Кешування розбиття шляху з захистом від не-строкових типів."""
    if not isinstance(path, str):
        path = str(path) if path is not None else ""
    return tuple(path.split("."))


def get_path(data: Any, path: str) -> Any:
    current = data
    for part in _cached_split_path(path):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return None if current in EMPTY else current


def find(data: Any, field: str) -> Any:
    for path in FIELD_SYNONYMS.get(field, []):
        value = get_path(data, path)
        if value is not None:
            return value
    return None


def to_text(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        return to_number(value.get("amount", value.get("value")))
    try:
        return float(str(value).replace(",", ".").strip())
    except ValueError:
        return None


def money(amount: Any, currency: str) -> dict:
    code = (currency or "UAH").upper()
    value = round(to_number(amount) or 0.0, 2)
    if code in CURRENCY_PREFIX:
        formatted = f"{CURRENCY_PREFIX[code]}{value:.2f}"
    else:
        formatted = f"{value:.2f} {'грн' if code == 'UAH' else code}"
    return {"amount": value, "currency": code, "formatted": formatted}


def normalize_items(raw_items: Any, currency: str) -> list:
    items = []
    if not isinstance(raw_items, list):
        return items
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = to_text(find(item, "item_title")) or "Товар"
        variant = to_text(find(item, "item_variant"))
        if variant and variant.lower() != "default title":
            title = f"{title} ({variant})"
        quantity_value = to_number(find(item, "item_quantity"))
        quantity = int(quantity_value) if quantity_value and quantity_value > 0 else 1
        unit_price = to_number(find(item, "item_price")) or 0.0
        line_total = to_number(find(item, "item_line_total"))
        items.append({
            "id": to_text(find(item, "item_id")),
            "title": title,
            "sku": to_text(find(item, "item_sku")),
            "quantity": quantity,
            "unit_price": money(unit_price, currency),
            "line_total": money(line_total if line_total is not None else unit_price * quantity, currency),
        })
    return items


def string_address(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in ("delivery_address", "deliveryAddress", "shipping_address", "shippingAddress", "address"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def deep_find(data: Any, hints: tuple, max_depth: int = 3, current_depth: int = 0) -> str:
    """Глибокий пошук з обмеженням глибини для запобігання переповнення стеку."""
    if current_depth > max_depth:
        return ""
        
    if isinstance(data, dict):
        for key, value in data.items():
            if any(hint in str(key).lower() for hint in hints) and to_text(value):
                return to_text(value)
        candidates = list(data.values())
    elif isinstance(data, list):
        candidates = data
    else:
        return ""
        
    for value in candidates:
        if isinstance(value, (dict, list)):
            found = deep_find(value, hints, max_depth, current_depth + 1)
            if found:
                return found
    return ""


def normalize_order(raw: dict, shop: str) -> dict:
    currency = to_text(find(raw, "currency")) or "UAH"

    first = (
        to_text(find(raw, "first_name")) 
        or to_text(get_path(raw, "customer.first_name")) 
        or to_text(get_path(raw, "shipping_address.first_name"))
    )
    last = (
        to_text(find(raw, "last_name")) 
        or to_text(get_path(raw, "customer.last_name")) 
        or to_text(get_path(raw, "shipping_address.last_name"))
    )
    customer_name = (
        " ".join(part for part in (first, last) if part) 
        or to_text(get_path(raw, "customer.name"))
        or to_text(get_path(raw, "shipping_address.name"))
        or to_text(find(raw, "customer_name"))
    )

    street = (
        to_text(get_path(raw, "shipping_address.address1")) 
        or to_text(get_path(raw, "shipping_address.street")) 
        or to_text(find(raw, "street"))
    )
    street2 = (
        to_text(get_path(raw, "shipping_address.address2")) 
        or to_text(find(raw, "street2"))
    )
    full_street = " ".join(part for part in (street, street2) if part) or string_address(raw)

    city = to_text(get_path(raw, "shipping_address.city")) or to_text(find(raw, "city"))
    region = to_text(get_path(raw, "shipping_address.province")) or to_text(get_path(raw, "shipping_address.state")) or to_text(find(raw, "region"))
    postal_code = to_text(get_path(raw, "shipping_address.zip")) or to_text(get_path(raw, "shipping_address.postal_code")) or to_text(find(raw, "postal_code"))
    country = to_text(get_path(raw, "shipping_address.country")) or to_text(find(raw, "country"))
    
    full_address = ", ".join(part for part in (country, region, city, full_street, postal_code) if part)

    phone = (
        to_text(get_path(raw, "shipping_address.phone")) 
        or to_text(get_path(raw, "customer.phone"))
        or to_text(find(raw, "phone")) 
        or deep_find(raw, ("phone",))
    )

    email = (
        to_text(get_path(raw, "customer.email"))
        or to_text(get_path(raw, "shipping_address.email"))
        or to_text(find(raw, "email")) 
        or deep_find(raw, ("email",))
    )

    chat_history = raw.get("chat_history") or raw.get("dialog_history") or ""

    return {
        "shop": shop,
        "id": to_text(get_path(raw, "id")) or to_text(find(raw, "order_number")),
        "order_number": to_text(find(raw, "order_number")),
        "created_at": to_text(find(raw, "created_at")),
        "currency": currency.upper(),
        "customer_name": customer_name,
        "phone": phone,
        "email": email,
        "delivery_method": to_text(find(raw, "delivery_method")),
        "delivery_address": {
            "street": full_street,
            "city": city,
            "region": region,
            "postal_code": postal_code,
            "country": country,
            "full": full_address,
        },
        "total_amount": money(find(raw, "total_amount"), currency),
        "items": normalize_items(find(raw, "items"), currency),
        "status": {
            "text": to_text(find(raw, "status")) or "В обробці",
            "cancelled": bool(find(raw, "cancelled")),
        },
        "chat_history": str(chat_history),
    }


def shopify_error(status_code: int, shop: str) -> OrderAPIError:
    if status_code == 401:
        return OrderAPIError(f"Невірний або недійсний API-токен для магазину «{shop}».", 401)
    if status_code == 403:
        return OrderAPIError(f"Токен для магазину «{shop}» не має необхідних прав (read_orders).", 403)
    if status_code == 404:
        return OrderAPIError(f"Магазин «{shop}» не знайдено.", 404)
    if status_code == 429:
        return OrderAPIError(f"Магазин «{shop}» тимчасово обмежив запити.", 429)
    return OrderAPIError(f"Помилка API магазину «{shop}»: код {status_code}.", 502)


async def fetch_orders(shop: str, token: str, orders_url: Optional[str] = None) -> list:
    """АСИНХРОННЕ отримання замовлень через httpx для одного магазину."""
    url = orders_url or f"https://{shop}/admin/api/2026-10/orders.json?status=any"
    headers = {"X-Shopify-Access-Token": token, "Authorization": f"Bearer {token}", "Accept": "application/json"}
    
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.get(url, headers=headers)
    except httpx.TimeoutException as error:
        raise OrderAPIError(f"Час очікування запиту до магазину «{shop}» вичерпано.", 504) from error
    except httpx.RequestError as error:
        raise OrderAPIError(f"Не вдалося підключитися до магазину «{shop}».", 502) from error

    if response.status_code != 200:
        raise shopify_error(response.status_code, shop)

    try:
        data = response.json()
    except ValueError as error:
        raise OrderAPIError(f"Магазин «{shop}» повернув невалідний JSON.", 502) from error

    if isinstance(data, list):
        return data
    return data.get("orders") or data.get("data") or data.get("items") or []


async def fetch_orders_multi(stores: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """ПАРАЛЕЛЬНИЙ збір замовлень з УСІХ підключених магазинів користувача (Multi-store fetch).
    stores — список об'єктів у форматі: [{"shop": "store1.com", "token": "tok1"}, {"shop": "store2.com", "token": "tok2"}]
    """
    if not isinstance(stores, list) or not stores:
        return []

    async def fetch_single_store(store_info: Dict[str, str]) -> List[Dict[str, Any]]:
        shop_raw = store_info.get("shop") or store_info.get("shop_domain")
        token = store_info.get("token") or store_info.get("access_token")
        
        shop = normalize_shop(shop_raw)
        if not shop or not token:
            logger.warning("[WARNING] Пропущено магазин через невалідні дані: %s", store_info)
            return []

        try:
            raw_orders = await fetch_orders(shop, token)
            # Нормалізуємо кожне замовлення з прив'язкою до цього магазину
            return [normalize_order(raw, shop) for raw in raw_orders if isinstance(raw, dict)]
        except Exception as e:
            logger.error("[ERROR] Не вдалося завантажити замовлення для магазину %s: %s", shop, e)
            return []

    # Запускаємо запити до всіх магазинів паралельно через asyncio.gather
    results = await asyncio.gather(*(fetch_single_store(store) for store in stores))
    
    # Зводимо всі списки замовлень в єдиний спільний масив
    all_orders = []
    for store_orders in results:
        all_orders.extend(store_orders)
        
    return all_orders