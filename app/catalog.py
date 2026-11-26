from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    title: str
    unit_kopecks: int


@dataclass(frozen=True)
class Catalog:
    merchant_id: str
    title: str
    products: dict[str, Product]
    deposit_percent: int = 100

    def price(self, quantities: dict[str, int]) -> dict:
        items = [
            {
                "sku": sku,
                "title": self.products[sku].title,
                "quantity": quantity,
                "unit_kopecks": self.products[sku].unit_kopecks,
                "amount_kopecks": quantity * self.products[sku].unit_kopecks,
            }
            for sku, quantity in quantities.items()
        ]
        total = sum(item["amount_kopecks"] for item in items)
        payable = (total * self.deposit_percent + 50) // 100
        return {
            "items": items,
            "total_kopecks": total,
            "payable_kopecks": payable,
            "balance_kopecks": total - payable,
            "currency": "RUB",
        }


COFFEE = Catalog(
    merchant_id="coffee",
    title="Кофейня «Утро»",
    products={
        "breakfast": Product("Обычный завтрак", 35_000),
        "vegetarian": Product("Вегетарианский завтрак", 40_000),
    },
)
